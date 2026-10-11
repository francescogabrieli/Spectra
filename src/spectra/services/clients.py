"""Typed clients for service-to-service calls on the private local network."""

from __future__ import annotations

import logging
import os
import sqlite3
import tempfile
from decimal import Decimal
from pathlib import Path
from typing import Any

import httpx
from pydantic import BaseModel

from spectra.config import Settings, load_settings
from spectra.services.contracts import (
    CategorizeRequest,
    CommitImportRequest,
    FxConvertRequest,
    ImportConfirmRequest,
    LEDGER_OPERATIONS,
    OperationRequest,
)

logger = logging.getLogger("spectra.services.client")

class ServiceUnavailable(RuntimeError):
    """An internal local service could not be reached or returned an error."""

class ServiceResponseError(RuntimeError):
    """An internal service returned a readable non-success response."""

    def __init__(self, status_code: int, message: str) -> None:
        super().__init__(message)
        self.status_code = status_code


def _json_value(value: Any) -> Any:
    if isinstance(value, Decimal):
        return format(value, "f")
    if isinstance(value, BaseModel):
        return value.model_dump(mode="json")
    if isinstance(value, dict):
        return {str(key): _json_value(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_json_value(item) for item in value]
    return value


class _SyncServiceClient:
    def __init__(self, base_url: str, *, timeout: float = 30.0) -> None:
        self.base_url = base_url.rstrip("/")
        self._http = httpx.Client(base_url=self.base_url, timeout=timeout)

    def close(self) -> None:
        self._http.close()

    def __enter__(self):
        return self

    def __exit__(self, *_exc):
        self.close()

    def _post(self, path: str, body: BaseModel | dict[str, Any]) -> Any:
        payload = _json_value(body)
        try:
            response = self._http.post(path, json=payload)
            response.raise_for_status()
            content = response.json()
            return content.get("result") if isinstance(content, dict) and "result" in content else content
        except (httpx.HTTPError, ValueError) as exc:
            logger.warning("Internal service request failed (%s)", type(exc).__name__)
            raise ServiceUnavailable(f"Internal service at {self.base_url} is unavailable") from exc

    def _get(self, path: str) -> Any:
        try:
            response = self._http.get(path)
            response.raise_for_status()
            return response.json()
        except (httpx.HTTPError, ValueError) as exc:
            logger.warning("Internal service request failed (%s)", type(exc).__name__)
            raise ServiceUnavailable(f"Internal service at {self.base_url} is unavailable") from exc


class LedgerClient(_SyncServiceClient):
    """Allowlisted ledger HTTP client; it never opens the ledger SQLite file."""

    def __init__(self, settings: Settings | None = None) -> None:
        settings = settings or load_settings()
        super().__init__(settings.ledger_url, timeout=settings.internal_api_timeout_seconds)

    def operation(self, name: str, *args: Any, **kwargs: Any) -> Any:
        if name not in LEDGER_OPERATIONS:
            raise AttributeError(f"Ledger operation is not exposed: {name}")
        result = self._post(
            f"/internal/v1/ledger/operations/{name}",
            OperationRequest(args=list(args), kwargs=kwargs),
        )
        if name == "get_budget_limits_exact" and isinstance(result, dict):
            from decimal import Decimal
            return {str(key): Decimal(str(value)) for key, value in result.items()}
        return result

    def read_model(self, name: str, **params: Any) -> Any:
        return self._post(
            f"/internal/v1/ledger/views/{name}",
            OperationRequest(kwargs=params),
        )

    def commit_import(self, request: CommitImportRequest) -> dict[str, Any]:
        return self._post("/internal/v1/ledger/imports/commit", request)

    def download_backup(self, destination: str | Path, *, max_bytes: int) -> Path:
        """Stream a canonical ledger snapshot to a new, private local file."""
        target = Path(destination)
        if target.exists():
            raise FileExistsError(f"Backup destination already exists: {target}")
        target.parent.mkdir(parents=True, exist_ok=True)
        descriptor, temporary_name = tempfile.mkstemp(
            prefix=f".{target.name}.", suffix=".tmp", dir=target.parent,
        )
        temporary = Path(temporary_name)
        total = 0
        try:
            os.chmod(temporary, 0o600)
            with os.fdopen(descriptor, "wb") as output:
                with self._http.stream("GET", "/internal/v1/ledger/backup") as response:
                    response.raise_for_status()
                    for chunk in response.iter_bytes(1024 * 1024):
                        total += len(chunk)
                        if total > max_bytes:
                            raise ValueError("Ledger backup exceeds the configured size limit")
                        output.write(chunk)
                output.flush()
                os.fsync(output.fileno())
            uri = f"{temporary.resolve().as_uri()}?mode=ro"
            with sqlite3.connect(uri, uri=True) as validation:
                check = validation.execute("PRAGMA quick_check").fetchone()
                tables = {
                    row[0] for row in validation.execute(
                        "SELECT name FROM sqlite_master WHERE type = 'table'",
                    )
                }
                if not check or check[0] != "ok" or not {"tx_history", "seen_transactions"} <= tables:
                    raise ValueError("Ledger returned an invalid database backup")
            os.link(temporary, target)
            return target
        except (httpx.HTTPError, sqlite3.Error) as exc:
            raise ServiceUnavailable("Ledger backup endpoint is unavailable") from exc
        finally:
            temporary.unlink(missing_ok=True)

    def __getattr__(self, name: str):
        if name in LEDGER_OPERATIONS:
            return lambda *args, **kwargs: self.operation(name, *args, **kwargs)
        raise AttributeError(name)


class EnrichmentClient(_SyncServiceClient):
    """HTTP adapter for categorization and fail-closed FX conversion."""

    def __init__(self, settings: Settings | None = None) -> None:
        settings = settings or load_settings()
        super().__init__(settings.enrichment_url, timeout=settings.internal_api_timeout_seconds)

    def categorize(self, request: CategorizeRequest) -> list[dict[str, Any]]:
        return self._post("/internal/v1/enrichment/categorize", request)

    def provider_status(self) -> dict[str, Any]:
        return self._get("/internal/v1/enrichment/provider-status")

    def convert(self, request: FxConvertRequest) -> dict[str, Any]:
        try:
            response = self._http.post(
                "/internal/v1/enrichment/fx/convert", json=_json_value(request),
            )
            if response.status_code == 422:
                from spectra.fx import FXConversionError
                detail = response.json().get("detail", "FX conversion unavailable")
                raise FXConversionError(str(detail))
            response.raise_for_status()
            return response.json()
        except httpx.HTTPError as exc:
            logger.warning("FX service request failed (%s)", type(exc).__name__)
            raise ServiceUnavailable("FX enrichment service is unavailable") from exc


class ImportClient:
    """Async HTTP client used by gateway routes and the command-line adapter."""

    def __init__(self, settings: Settings | None = None) -> None:
        self.settings = settings or load_settings()
        self.base_url = self.settings.import_service_url.rstrip("/")
        self.timeout = self.settings.internal_api_timeout_seconds

    async def create_job_stream(self, filename: str, content: bytes, base_currency: str):
        async with httpx.AsyncClient(base_url=self.base_url, timeout=None) as client:
            try:
                async with client.stream(
                    "POST",
                    "/internal/v1/imports",
                    data={"base_currency": base_currency},
                    files={"file": (filename, content, "application/octet-stream")},
                ) as response:
                    response.raise_for_status()
                    async for chunk in response.aiter_text():
                        yield chunk
            except httpx.HTTPError as exc:
                logger.warning("Import service request failed (%s)", type(exc).__name__)
                raise ServiceUnavailable("Import service is unavailable") from exc

    async def confirm(self, job_id: str | None, transactions: list[dict[str, Any]]):
        async with httpx.AsyncClient(base_url=self.base_url, timeout=self.timeout) as client:
            try:
                path = (
                    f"/internal/v1/imports/{job_id}/confirm"
                    if job_id else "/internal/v1/imports/legacy-confirm"
                )
                response = await client.post(
                    path,
                    json=ImportConfirmRequest(transactions=transactions).model_dump(mode="json"),
                )
                response.raise_for_status()
                return response.json()
            except httpx.HTTPStatusError as exc:
                try:
                    body = exc.response.json()
                except ValueError:
                    body = {}
                message = (
                    body.get("detail") or body.get("message") or body.get("error")
                    if isinstance(body, dict) else None
                )
                if not message:
                    message = "Importer rejected the confirmation request"
                raise ServiceResponseError(exc.response.status_code, str(message)) from exc
            except (httpx.RequestError, ValueError) as exc:
                raise ServiceUnavailable("Import service is unavailable") from exc

    async def cli_import(
        self, filename: str, content: bytes, base_currency: str, dry_run: bool,
    ) -> dict[str, Any]:
        async with httpx.AsyncClient(base_url=self.base_url, timeout=None) as client:
            try:
                response = await client.post(
                    "/internal/v1/imports/cli",
                    data={"base_currency": base_currency, "dry_run": str(dry_run).lower()},
                    files={"file": (filename, content, "application/octet-stream")},
                )
                response.raise_for_status()
                return response.json()
            except (httpx.HTTPError, ValueError) as exc:
                raise ServiceUnavailable("Import service is unavailable") from exc

