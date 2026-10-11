"""Private versioned API for the one authoritative financial ledger."""

from __future__ import annotations

import logging
import tempfile
from pathlib import Path
from decimal import Decimal
from typing import Any

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse
from starlette.background import BackgroundTask

from spectra.ai import CategorisedTransaction
from spectra.backup import create_backup
from spectra.config import load_settings
from spectra.db import BookmarkDB
from spectra.services.contracts import (
    CommitImportRequest,
    LEDGER_OPERATIONS,
    OperationRequest,
    OperationResponse,
)

logger = logging.getLogger("spectra.services.ledger")
app = FastAPI(title="Spectra Ledger", version="1.0", docs_url=None, redoc_url=None)


def _wire(value: Any) -> Any:
    if isinstance(value, Decimal):
        return format(value, "f")
    if isinstance(value, dict):
        return {str(key): _wire(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_wire(item) for item in value]
    return value


def _settings():
    return load_settings()


def _as_transactions(values: list[dict[str, Any]]) -> list[CategorisedTransaction]:
    return [CategorisedTransaction.model_validate(value) for value in values]


@app.get("/health/live")
def live() -> dict[str, str]:
    return {"service": "ledger", "status": "live"}


@app.get("/health/ready")
def ready() -> dict[str, str]:
    settings = _settings()
    try:
        with BookmarkDB(settings.db_path) as db:
            check = db._conn.execute("PRAGMA quick_check").fetchone()
            if not check or check[0] != "ok":
                raise RuntimeError("database integrity check failed")
    except Exception as exc:
        logger.error("Ledger readiness check failed (%s)", type(exc).__name__)
        raise HTTPException(status_code=503, detail="Ledger database is unavailable") from exc
    return {"service": "ledger", "status": "ready"}


@app.post("/internal/v1/ledger/operations/{name}", response_model=OperationResponse)
def operation(name: str, request: OperationRequest) -> OperationResponse:
    if name not in LEDGER_OPERATIONS:
        raise HTTPException(status_code=404, detail="Ledger operation is not available")
    settings = _settings()
    args = list(request.args)
    kwargs = dict(request.kwargs)
    if name in {"save_import_atomic", "commit_import_once"} and args:
        raise HTTPException(status_code=400, detail="Invalid ledger operation payload")
    if name == "save_import_atomic":
        args = [_as_transactions(args[0])] if args else args
    try:
        with BookmarkDB(settings.db_path) as db:
            result = getattr(db, name)(*args, **kwargs)
    except (TypeError, ValueError, KeyError) as exc:
        raise HTTPException(status_code=400, detail="Invalid ledger operation data") from exc
    except Exception as exc:
        logger.exception("Ledger operation failed (%s)", name)
        raise HTTPException(status_code=500, detail="Ledger operation failed") from exc
    return OperationResponse(result=_wire(result))


@app.post("/internal/v1/ledger/views/{name}", response_model=OperationResponse)
def read_model(name: str, request: OperationRequest) -> OperationResponse:
    settings = _settings()
    try:
        with BookmarkDB(settings.db_path) as db:
            result = db.read_model(name, **request.kwargs)
    except (TypeError, ValueError, KeyError) as exc:
        raise HTTPException(status_code=400, detail="Invalid ledger read model request") from exc
    except Exception as exc:
        logger.exception("Ledger read model failed (%s)", name)
        raise HTTPException(status_code=500, detail="Ledger read model failed") from exc
    return OperationResponse(result=_wire(result))


@app.post("/internal/v1/ledger/imports/commit")
def commit_import(request: CommitImportRequest) -> dict[str, Any]:
    settings = _settings()
    try:
        with BookmarkDB(settings.db_path) as db:
            result = db.commit_import_once(
                request.idempotency_key,
                _as_transactions(request.transactions),
                learning_feedback=request.learning_feedback,
                merchant_mappings=request.merchant_mappings,
                overrides=request.overrides,
                existing_transfer_updates=request.existing_transfer_updates,
            )
    except (TypeError, ValueError, KeyError) as exc:
        raise HTTPException(status_code=400, detail="Invalid import commit") from exc
    except Exception as exc:
        logger.exception("Atomic import commit failed")
        raise HTTPException(status_code=500, detail="Import commit failed") from exc
    return result


@app.get("/internal/v1/ledger/backup")
def backup_database():
    settings = _settings()
    directory = Path(tempfile.mkdtemp(prefix="spectra-ledger-backup-"))
    destination = directory / "spectra-backup.sqlite"
    try:
        create_backup(settings.db_path, destination)
    except Exception as exc:
        destination.unlink(missing_ok=True)
        directory.rmdir()
        raise HTTPException(status_code=400, detail="Ledger database backup failed") from exc
    def cleanup() -> None:
        destination.unlink(missing_ok=True)
        directory.rmdir()

    return FileResponse(
        destination, media_type="application/octet-stream", filename="spectra-backup.sqlite",
        background=BackgroundTask(cleanup),
    )

