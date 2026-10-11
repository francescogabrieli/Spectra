"""Import API: parse, enrich, stage review drafts, and request ledger commits."""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import tempfile
import threading
from pathlib import Path
from typing import Any

import httpx
from fastapi import FastAPI, File, Form, HTTPException, UploadFile
from fastapi.responses import StreamingResponse
from starlette.background import BackgroundTask

from spectra.ai import CategorisedTransaction
from spectra.config import load_settings
from spectra.import_service import parse_file, prepare_import
from spectra.dashboard import refresh_dashboard
from spectra.sheets import SheetsClient
from spectra.services.clients import EnrichmentClient, LedgerClient, ServiceUnavailable
from spectra.services.contracts import CommitImportRequest, ImportConfirmRequest
from spectra.services.importer.confirmation import confirm_review
from spectra.services.importer.store import ImportJobStore

logger = logging.getLogger("spectra.services.importer")
app = FastAPI(title="Spectra Import", version="1.0", docs_url=None, redoc_url=None)
_IMPORT_SLOTS = threading.BoundedSemaphore(2)


def _store() -> ImportJobStore:
    return ImportJobStore(load_settings().import_db_path)


def _preview_transaction(
    transaction: CategorisedTransaction, provider: str,
) -> dict[str, Any]:
    result = transaction.model_dump(mode="json")
    result["merchant"] = result.pop("clean_name")
    result["apply_to_future"] = True
    if provider != "local":
        result.pop("category_confidence", None)
        result.pop("category_suggestions", None)
    return result


def _validate_file(filename: str, content: bytes, max_bytes: int) -> str:
    suffix = Path(filename or "statement.csv").suffix.lower()
    if suffix not in {".csv", ".pdf", ".ofx"}:
        raise HTTPException(status_code=400, detail="Upload one of CSV, PDF, or OFX files")
    if len(content) > max_bytes:
        raise HTTPException(status_code=413, detail="Upload exceeds the configured size limit")
    if not content:
        raise HTTPException(status_code=400, detail="The uploaded file is empty")
    return suffix


def _sheets_configured(settings) -> bool:
    return bool(
        settings.spreadsheet_id
        and (
            settings.google_sheets_credentials_b64
            or Path(settings.google_sheets_credentials_file).exists()
        )
    )


def _prepare(
    content: bytes,
    suffix: str,
    base_currency: str,
    progress=None,
    *,
    persist_fx: bool = True,
    include_sheets_categories: bool = False,
    import_batch_id: str | None = None,
):
    settings = load_settings()
    with tempfile.TemporaryDirectory(prefix="spectra-import-") as directory:
        path = Path(directory) / f"statement{suffix}"
        path.write_bytes(content)
        parsed = parse_file(path, currency=base_currency)
        existing_categories = None
        if include_sheets_categories and _sheets_configured(settings):
            try:
                sheets = SheetsClient(
                    spreadsheet_id=settings.spreadsheet_id,
                    credentials_b64=settings.google_sheets_credentials_b64,
                    credentials_file=settings.google_sheets_credentials_file,
                )
                existing_categories = sheets.get_existing_categories()
            except Exception as exc:
                logger.warning(
                    "Optional Sheets categories unavailable (%s); using local ledger categories",
                    type(exc).__name__,
                )
        with LedgerClient(settings) as ledger, EnrichmentClient(settings) as enrichment:
            prepared = prepare_import(
                parsed,
                settings=settings,
                db=ledger,
                existing_categories=existing_categories,
                import_batch_id=import_batch_id,
                base_currency=base_currency,
                progress=progress,
                enrichment_client=enrichment,
                persist_fx_cache=persist_fx,
            )
    return prepared


def _sync_sheets(settings, transactions: list[CategorisedTransaction], currency: str) -> bool:
    """Optional adapter; the local ledger commit always remains authoritative."""
    if not transactions or not _sheets_configured(settings):
        return False
    try:
        sheets = SheetsClient(
            spreadsheet_id=settings.spreadsheet_id,
            credentials_b64=settings.google_sheets_credentials_b64,
            credentials_file=settings.google_sheets_credentials_file,
        )
        sheets.append_transactions(transactions)
        refresh_dashboard(sheets, currency)
        return True
    except Exception as exc:
        logger.warning(
            "Local import saved; optional Google Sheets sync failed (%s)",
            type(exc).__name__,
        )
        return False


@app.get("/health/live")
def live() -> dict[str, str]:
    return {"service": "importer", "status": "live"}


@app.get("/health/ready")
def ready() -> dict[str, Any]:
    settings = load_settings()
    states: dict[str, str] = {}
    for name, url in (("ledger", settings.ledger_url), ("enrichment", settings.enrichment_url)):
        try:
            response = httpx.get(f"{url.rstrip('/')}/health/ready", timeout=2.0)
            response.raise_for_status()
            states[name] = "ready"
        except httpx.HTTPError:
            states[name] = "unavailable"
    if any(state != "ready" for state in states.values()):
        raise HTTPException(status_code=503, detail={"service": "importer", "dependencies": states})
    _store()
    return {"service": "importer", "status": "ready", "dependencies": states}


@app.post("/internal/v1/imports")
async def create_review_job(
    file: UploadFile = File(...), base_currency: str = Form(...),
):
    settings = load_settings()
    content = await file.read(settings.max_upload_bytes + 1)
    suffix = _validate_file(file.filename or "statement.csv", content, settings.max_upload_bytes)
    currency = str(base_currency).strip().upper()
    if len(currency) != 3 or not currency.isalpha():
        raise HTTPException(status_code=400, detail="A valid base currency is required")
    if not _IMPORT_SLOTS.acquire(blocking=False):
        raise HTTPException(status_code=429, detail="Another import is in progress. Try again shortly.")

    slot_state = {"deferred": False, "finished": False, "released": False}
    slot_lock = threading.Lock()

    def release_slot() -> None:
        with slot_lock:
            if slot_state["deferred"] and not slot_state["finished"]:
                return
            if slot_state["released"]:
                return
            slot_state["released"] = True
        _IMPORT_SLOTS.release()

    def defer_release() -> None:
        with slot_lock:
            slot_state["deferred"] = True

    def finish_deferred_work() -> None:
        with slot_lock:
            slot_state["finished"] = True
        release_slot()

    async def stream_events():
        loop = asyncio.get_running_loop()
        updates: asyncio.Queue[tuple[int, str]] = asyncio.Queue()

        def report(pct: int, label: str) -> None:
            try:
                loop.call_soon_threadsafe(updates.put_nowait, (pct, label))
            except RuntimeError:
                pass

        def work():
            prepared = _prepare(content, suffix, currency, report)
            rows = [
                _preview_transaction(tx, settings.ai_provider)
                for tx in prepared.transactions
            ]
            if rows:
                _store().create(prepared.import_batch_id, currency, rows)
            return prepared, rows

        def event(pct: int, step: str, **extra) -> str:
            return f"data: {json.dumps({'pct': pct, 'step': step, **extra})}\n\n"

        task = asyncio.create_task(asyncio.to_thread(work))
        yield event(5, "Upload received")
        try:
            while not task.done():
                update_task = asyncio.create_task(updates.get())
                done, _pending = await asyncio.wait(
                    {task, update_task}, return_when=asyncio.FIRST_COMPLETED,
                )
                if update_task in done:
                    pct, label = update_task.result()
                    yield event(pct, label)
                else:
                    update_task.cancel()
            while not updates.empty():
                pct, label = updates.get_nowait()
                yield event(pct, label)
            prepared, rows = task.result()
            yield event(
                100,
                f"{len(rows)} transaction(s) ready",
                done=True,
                job_id=prepared.import_batch_id,
                transactions=rows,
                message=f"{len(rows)} new transaction(s); {prepared.skipped_duplicates} duplicate(s) skipped",
                review_count=sum(bool(tx.needs_review) for tx in prepared.transactions),
                transfer_count=sum(tx.transfer_status == "suggested" for tx in prepared.transactions),
                skipped_duplicates=prepared.skipped_duplicates,
            )
        except asyncio.CancelledError:
            if not task.done():
                defer_release()

                def release_when_finished(completed: asyncio.Task) -> None:
                    if not completed.cancelled():
                        completed.exception()
                    finish_deferred_work()

                task.add_done_callback(release_when_finished)
            raise
        except ServiceUnavailable as exc:
            yield event(0, str(exc), error=True)
        except Exception as exc:
            logger.warning("Import preparation failed (%s)", type(exc).__name__)
            yield event(0, "The statement could not be processed. Check its format and settings.", error=True)
        finally:
            release_slot()

    return StreamingResponse(stream_events(), media_type="text/event-stream", headers={
        "Cache-Control": "no-cache", "X-Accel-Buffering": "no",
    }, background=BackgroundTask(release_slot))


@app.get("/internal/v1/imports/{job_id}")
def get_review_job(job_id: str) -> dict[str, Any]:
    job = _store().get(job_id)
    if job is None:
        raise HTTPException(status_code=404, detail="Import review job not found or expired")
    return job


@app.post("/internal/v1/imports/{job_id}/confirm")
def confirm_review_job(job_id: str, request: ImportConfirmRequest) -> dict[str, Any]:
    job = _store().get(job_id)
    if job is None:
        raise HTTPException(status_code=404, detail="Import review job not found or expired")
    if job["status"] == "committed" and job["result"]:
        return job["result"]
    submitted = request.transactions
    settings = load_settings()
    try:
        with LedgerClient(settings) as ledger:
            result = confirm_review(
                ledger,
                job["transactions"],
                submitted,
                base_currency=job["base_currency"],
                idempotency_key=job_id,
            )
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except ServiceUnavailable as exc:
        raise HTTPException(status_code=503, detail="Ledger service is unavailable; retry confirmation") from exc
    _store().mark_committed(job_id, result)
    return result


@app.post("/internal/v1/imports/legacy-confirm")
def confirm_legacy_payload(request: ImportConfirmRequest) -> dict[str, Any]:
    """Compatibility endpoint for clients predating staged review job IDs."""
    settings = load_settings()
    payloads = request.transactions
    if not payloads:
        raise HTTPException(status_code=400, detail="No transactions to save")
    digest = hashlib.sha256(
        "|".join(sorted(str(row.get("id", "")) for row in payloads)).encode("utf-8")
    ).hexdigest()
    try:
        with LedgerClient(settings) as ledger:
            base_currency = str(ledger.get_app_setting("base_currency") or settings.base_currency).upper()
            result = confirm_review(
                ledger, payloads, payloads, base_currency=base_currency,
                idempotency_key=f"legacy-confirm:{digest}",
            )
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except ServiceUnavailable as exc:
        raise HTTPException(status_code=503, detail="Ledger service is unavailable; retry confirmation") from exc
    return result


@app.post("/internal/v1/imports/cli")
async def cli_import(
    file: UploadFile = File(...),
    base_currency: str = Form(...),
    dry_run: bool = Form(False),
) -> dict[str, Any]:
    settings = load_settings()
    content = await file.read(settings.max_upload_bytes + 1)
    suffix = _validate_file(file.filename or "statement.csv", content, settings.max_upload_bytes)
    currency = str(base_currency).strip().upper()
    if not _IMPORT_SLOTS.acquire(blocking=False):
        raise HTTPException(status_code=429, detail="Another import is in progress. Try again shortly.")
    try:
        import_key = "cli-" + hashlib.sha256(
            currency.encode("ascii") + b"\0" + content
        ).hexdigest()
        prepared = await asyncio.to_thread(
            _prepare,
            content,
            suffix,
            currency,
            None,
            persist_fx=not dry_run,
            include_sheets_categories=not dry_run,
            import_batch_id=import_key,
        )
        if dry_run:
            return {
                "ok": True,
                "dry_run": True,
                "transactions": [tx.model_dump(mode="json") for tx in prepared.transactions],
                "skipped_duplicates": prepared.skipped_duplicates,
            }
        def commit_locally() -> dict[str, Any]:
            with LedgerClient(settings) as ledger:
                return ledger.commit_import(
                    CommitImportRequest(
                        idempotency_key=prepared.import_batch_id,
                        transactions=[tx.model_dump(mode="json") for tx in prepared.transactions],
                        merchant_mappings={
                            tx.clean_name: tx.category for tx in prepared.transactions
                            if tx.category != "Uncategorized"
                        },
                        existing_transfer_updates=prepared.transfer_updates,
                    )
                )

        result = await asyncio.to_thread(commit_locally)
        if not result.get("already_committed"):
            sheets_synced = await asyncio.to_thread(
                _sync_sheets, settings, prepared.transactions, currency,
            )
        else:
            sheets_synced = False
        return {
            **result,
            "ok": True,
            "dry_run": False,
            "message": f"Saved {len(prepared.transactions)} transaction(s) to local ledger",
            "skipped_duplicates": prepared.skipped_duplicates,
            "sheets_synced": sheets_synced,
        }
    except ServiceUnavailable as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    finally:
        _IMPORT_SLOTS.release()

