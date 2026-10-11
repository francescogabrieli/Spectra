from __future__ import annotations

import json
from datetime import date, timedelta
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from spectra.ai import CategorySuggestion, CategorisedTransaction
from spectra.config import Settings
from spectra.db import BookmarkDB
from spectra.services.contracts import CategorizeRequest, CommitImportRequest, FxConvertRequest
from spectra.web import server


@pytest.fixture
def web_settings(tmp_path: Path) -> Settings:
    creds = tmp_path / "dummy.json"
    creds.write_text("{}")
    return Settings(
        ai_provider="local",
        spreadsheet_id="",
        google_sheets_credentials_file=str(creds),
        db_path=tmp_path / "web.db",
        import_db_path=tmp_path / "imports.sqlite",
        fx_db_path=tmp_path / "fx.sqlite",
        service_role="gateway",
        log_level="DEBUG",
    )


@pytest.fixture
def client(monkeypatch: pytest.MonkeyPatch, web_settings: Settings) -> TestClient:
    import asyncio
    import hashlib
    import json
    import tempfile
    from pathlib import Path

    from spectra import ai as ai_module
    from spectra.ai import CategorisedTransaction
    from spectra.fx import convert_currency_result
    from spectra import import_service
    from spectra.import_service import parse_file
    from spectra.services.importer.confirmation import confirm_review

    jobs: dict[str, dict[str, object]] = {}

    class LocalLedgerClient:
        def __init__(self, _settings=None) -> None:
            self.db = BookmarkDB(web_settings.db_path)

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            self.close()

        def close(self):
            self.db.close()

        def __getattr__(self, name):
            return getattr(self.db, name)

        def commit_import(self, request: CommitImportRequest):
            return self.db.commit_import_once(
                request.idempotency_key,
                [CategorisedTransaction.model_validate(row) for row in request.transactions],
                learning_feedback=request.learning_feedback,
                merchant_mappings=request.merchant_mappings,
                overrides=request.overrides,
                existing_transfer_updates=request.existing_transfer_updates,
            )

    class UnitEnrichmentClient:
        def categorize(self, request: CategorizeRequest):
            if request.provider == "local":
                from spectra import local_categorizer, ml_classifier

                classifier = ml_classifier.train_classifier(request.training_data)
                rows = local_categorizer.categorise_local(
                    request.transactions,
                    merchant_db=request.merchant_categories,
                    ml_classifier=classifier,
                )
            else:
                settings = server.load_settings()
                api_key = settings.openai_api_key if request.provider == "openai" else settings.gemini_api_key
                model = settings.openai_model if request.provider == "openai" else settings.gemini_model
                rows = ai_module.categorise(
                    request.transactions, request.categories, provider=request.provider,
                    api_key=api_key, model=model, base_currency=request.base_currency,
                )
            return [row.model_dump(mode="json") for row in rows]

        def convert(self, request: FxConvertRequest):
            result = convert_currency_result(
                request.amount, request.from_currency, request.to_currency,
                request.rate_date,
            )
            return {
                "amount": format(result.amount, "f"),
                "rate": format(result.rate, "f"),
                "requested_date": result.requested_date,
                "effective_date": result.effective_date,
                "source": result.source,
            }

    class UnitImportClient:
        def __init__(self, _settings=None) -> None:
            pass

        async def create_job_stream(self, filename, content, base_currency):
            def work():
                suffix = Path(filename).suffix or ".csv"
                with tempfile.TemporaryDirectory(prefix="spectra-test-import-") as directory:
                    source_path = Path(directory) / f"statement{suffix}"
                    source_path.write_bytes(content)
                    parsed = parse_file(source_path, currency=base_currency)
                    with LocalLedgerClient() as ledger:
                        return import_service.prepare_import(
                            parsed,
                            settings=server.load_settings(),
                            db=ledger,
                            base_currency=base_currency,
                            enrichment_client=UnitEnrichmentClient(),
                        )

            yield "data: {\"pct\": 10, \"step\": \"Test worker started\"}\n\n"
            try:
                prepared = await asyncio.to_thread(work)
            except Exception:
                yield "data: {\"pct\": 0, \"error\": true}\n\n"
                return
            rows = []
            for transaction in prepared.transactions:
                row = transaction.model_dump(mode="json")
                row["merchant"] = row.pop("clean_name")
                row["apply_to_future"] = True
                if server.load_settings().ai_provider != "local":
                    row.pop("category_confidence", None)
                    row.pop("category_suggestions", None)
                rows.append(row)
            jobs[prepared.import_batch_id] = {
                "transactions": rows,
                "base_currency": base_currency,
                "result": None,
            }
            yield "data: " + json.dumps({
                "pct": 100, "step": "Ready", "done": True,
                "job_id": prepared.import_batch_id, "transactions": rows,
                "skipped_duplicates": prepared.skipped_duplicates,
            }) + "\n\n"

        async def confirm(self, job_id, transactions):
            def persist():
                settings = server.load_settings()
                with LocalLedgerClient() as ledger:
                    if job_id:
                        job = jobs[job_id]
                        result = confirm_review(
                            ledger,
                            job["transactions"],
                            transactions,
                            base_currency=job["base_currency"],
                            idempotency_key=job_id,
                        )
                        job["result"] = result
                        return result
                    key = "legacy-" + hashlib.sha256(
                        "|".join(sorted(str(row.get("id", "")) for row in transactions)).encode()
                    ).hexdigest()
                    currency = str(ledger.get_app_setting("base_currency") or settings.base_currency)
                    return confirm_review(
                        ledger, transactions, transactions, base_currency=currency,
                        idempotency_key=key,
                    )

            return await asyncio.to_thread(persist)

    monkeypatch.setattr(server, "load_settings", lambda: web_settings)
    monkeypatch.setattr(server, "LedgerClient", LocalLedgerClient)
    monkeypatch.setattr(server, "ImportClient", UnitImportClient)
    return TestClient(server.app)


def seed_tx(
    db: BookmarkDB,
    *,
    tx_id: str,
    tx_date: str,
    merchant: str,
    amount: float,
    category: str,
    original_description: str,
    **extra: object,
) -> None:
    db.save_history(
        [
            SimpleNamespace(
                id=tx_id,
                date=tx_date,
                clean_name=merchant,
                amount=amount,
                category=category,
                original_description=original_description,
                **extra,
            )
        ]
    )


def parse_sse_events(raw: str) -> list[dict[str, object]]:
    events: list[dict[str, object]] = []
    for chunk in raw.split("\n\n"):
        data_line = next((line for line in chunk.splitlines() if line.startswith("data: ")), None)
        if not data_line:
            continue
        events.append(json.loads(data_line[6:]))
    return events


def test_patch_transaction_persists_learning(client: TestClient, web_settings: Settings) -> None:
    with BookmarkDB(web_settings.db_path) as db:
        seed_tx(
            db,
            tx_id="tx-1",
            tx_date="2026-03-10",
            merchant="Netflix.Com",
            amount=-12.99,
            category="Uncategorized",
            original_description="ADDEBITO SDD NETFLIX.COM",
        )

    response = client.patch(
        "/api/transactions/tx-1",
        json={"merchant": "Netflix", "category": "Digital Subscriptions", "apply_to_future": True},
    )
    assert response.status_code == 200
    assert response.json()["ok"] is True

    with BookmarkDB(web_settings.db_path) as db:
        row = db._conn.execute(
            "SELECT clean_name, category FROM tx_history WHERE tx_id = 'tx-1'"
        ).fetchone()
        assert row == ("Netflix", "Digital Subscriptions")
        assert db.get_merchant_categories()["Netflix"] == "Digital Subscriptions"
        assert db.get_overrides()["ADDEBITO SDD NETFLIX.COM"]["category"] == "Digital Subscriptions"

        learning = db.get_recent_learning_feedback(limit=5)
        assert learning[0]["source"] == "manual_edit"
        assert learning[0]["apply_to_future"] is True


def test_first_run_requires_currency_setup_redirect(client: TestClient) -> None:
    response = client.get("/", follow_redirects=False)
    assert response.status_code == 303
    assert response.headers["location"] == "/settings?setup=currency"


def test_settings_page_does_not_redirect_during_first_run(client: TestClient) -> None:
    response = client.get("/settings?setup=currency", follow_redirects=False)
    assert response.status_code == 200


def test_base_currency_can_be_set_via_preferences(client: TestClient, web_settings: Settings) -> None:
    response = client.patch(
        "/api/settings/preferences",
        json={"base_currency": "usd"},
    )
    assert response.status_code == 200
    payload = response.json()
    assert payload["ok"] is True
    assert payload["currency"] == "USD"
    assert payload["requires_base_currency_setup"] is False

    with BookmarkDB(web_settings.db_path) as db:
        assert db.get_app_setting("base_currency") == "USD"


def test_changing_base_currency_does_not_relabel_existing_transaction(
    client: TestClient,
    web_settings: Settings,
) -> None:
    with BookmarkDB(web_settings.db_path) as db:
        db.set_app_setting("base_currency", "EUR")
        seed_tx(
            db,
            tx_id="tx-eur-history",
            tx_date=date.today().isoformat(),
            merchant="Market",
            amount=-25.0,
            category="Food",
            original_description="MARKET",
            currency="EUR",
        )

    response = client.patch("/api/settings/preferences", json={"base_currency": "USD"})
    assert response.status_code == 200
    with BookmarkDB(web_settings.db_path) as db:
        row = db.get_transaction("tx-eur-history")
        assert row is not None
        assert row["currency"] == "EUR"
        assert row["amount"] == -25.0

    summary = client.get("/api/summary?scope=90d").json()
    assert summary["base_currency"] == "USD"
    assert summary["total_spent"] == 0
    assert summary["non_base_currency_count"] == 1


def test_legacy_currency_can_be_reviewed_and_confirmed_explicitly(
    client: TestClient,
    web_settings: Settings,
) -> None:
    import sqlite3

    conn = sqlite3.connect(web_settings.db_path)
    conn.execute(
        """CREATE TABLE tx_history (
            tx_id TEXT PRIMARY KEY, date TEXT NOT NULL, clean_name TEXT NOT NULL,
            amount REAL NOT NULL, category TEXT NOT NULL DEFAULT 'Uncategorized',
            original_description TEXT NOT NULL DEFAULT ''
        )"""
    )
    conn.execute(
        "INSERT INTO tx_history (tx_id, date, clean_name, amount, category, original_description) VALUES (?, ?, ?, ?, ?, ?)",
        ("old-row", "2025-04-10", "Market", -20.5, "Food", "MARKET"),
    )
    conn.commit()
    conn.close()

    # Initializing the application DB applies the real backwards-compatible migration.
    with BookmarkDB(web_settings.db_path) as db:
        db.set_app_setting("base_currency", "EUR")

    unresolved = client.get("/api/settings/legacy-currency")
    assert unresolved.status_code == 200
    assert unresolved.json()["total"] == 1
    assert unresolved.json()["items"][0]["id"] == "old-row"

    invalid = client.patch(
        "/api/settings/legacy-currency",
        json={"items": [{"id": "old-row", "currency": "EU"}]},
    )
    assert invalid.status_code == 400

    confirmed = client.patch(
        "/api/settings/legacy-currency",
        json={"items": [{"id": "old-row", "currency": "EUR"}]},
    )
    assert confirmed.status_code == 200
    assert confirmed.json()["confirmed"] == 1
    visible = client.get("/api/transactions?search=Market").json()["transactions"]
    assert len(visible) == 1
    assert visible[0]["currency"] == "EUR"
    assert visible[0]["currency_source"] == "user-confirmed"
    assert visible[0]["needs_review"] is False
    assert client.get("/api/settings/legacy-currency").json()["total"] == 0


def test_currency_aggregates_exclude_other_denominations(
    client: TestClient,
    web_settings: Settings,
) -> None:
    today = date.today().isoformat()
    with BookmarkDB(web_settings.db_path) as db:
        db.set_app_setting("base_currency", "EUR")
        seed_tx(
            db, tx_id="eur-sub", tx_date=today, merchant="Stream Co", amount=-10.0,
            category="Digital Subscriptions", original_description="STREAM CO", currency="EUR",
        )
        seed_tx(
            db, tx_id="usd-sub", tx_date=today, merchant="Stream Co", amount=-100.0,
            category="Digital Subscriptions", original_description="STREAM CO", currency="USD",
        )
        seed_tx(
            db, tx_id="legacy-unknown", tx_date=today, merchant="Old payment", amount=-90.0,
            category="Food", original_description="OLD PAYMENT",
        )
        db._conn.execute(
            """UPDATE tx_history SET amount_currency = NULL, currency_source = 'legacy-unknown',
               needs_review = 1, review_reason = 'legacy_currency_unknown' WHERE tx_id = ?""",
            ("legacy-unknown",),
        )
        db._conn.commit()

    summary = client.get("/api/summary?scope=90d").json()
    assert summary["total_spent"] == 10.0
    assert summary["subscriptions"] == 10.0
    assert summary["non_base_currency_count"] == 2

    budget = client.get("/api/budget").json()
    subscription_budget = next(item for item in budget["items"] if item["category"] == "Digital Subscriptions")
    assert subscription_budget["spent"] == 10.0
    assert budget["non_base_currency_count"] == 2

    trends = client.get("/api/trends").json()
    year = str(date.today().year)
    assert trends["by_year"][year]["expenses"] == 10.0
    assert trends["non_base_currency_count"] == 2

    subscriptions = client.get("/api/subscriptions").json()
    assert subscriptions["summary"]["active_count"] == 1
    assert subscriptions["items"][0]["last_amount"] == 10.0
    assert subscriptions["non_base_currency_count"] == 2


def test_unknown_currency_history_stays_visible_and_is_not_relabeled(
    client: TestClient,
    web_settings: Settings,
) -> None:
    today = date.today().isoformat()
    with BookmarkDB(web_settings.db_path) as db:
        db.set_app_setting("base_currency", "EUR")
        seed_tx(
            db, tx_id="legacy-unknown", tx_date=today, merchant="Old payment",
            amount=-1.2345, category="Food", original_description="OLD PAYMENT",
        )
        db._conn.execute(
            """UPDATE tx_history SET amount = -1.2345, amount_currency = NULL,
               amount_decimal = NULL, currency_source = 'legacy-unknown',
               needs_review = 1, review_reason = 'legacy_currency_unknown'
               WHERE tx_id = ?""",
            ("legacy-unknown",),
        )
        db._conn.commit()

    transactions = client.get("/api/transactions?search=Old%20payment").json()["transactions"]
    review = client.get("/api/review").json()["items"]
    assert len(transactions) == 1 and transactions[0]["currency"] is None
    assert len(review) == 1 and review[0]["currency"] is None

    summary = client.get("/api/summary?scope=90d").json()
    assert summary["total_spent"] == 0
    assert summary["non_base_currency_count"] == 1
    trends = client.get("/api/trends").json()
    assert trends["years"] == []
    assert trends["non_base_currency_count"] == 1

    trends_page = client.get("/trends").text
    assert "These records remain in Transactions and exports" in trends_page
    assert "excluded from ${currency} trends" in trends_page
    assert "currency unknown" in client.get("/review").text
    assert "unknown currency`" in client.get("/transactions").text


def test_zero_budget_limit_is_kept_as_a_visible_value(client: TestClient, web_settings: Settings) -> None:
    client.patch("/api/settings/preferences", json={"base_currency": "EUR"})
    today = date.today().isoformat()
    with BookmarkDB(web_settings.db_path) as db:
        seed_tx(
            db, tx_id="zero-budget-row", tx_date=today, merchant="Market", amount=-1,
            category="Food", original_description="MARKET", currency="EUR",
        )

    assert client.patch("/api/budget/Food", json={"limit": "0"}).status_code == 200
    budget = client.get("/api/budget").json()
    food = next(item for item in budget["items"] if item["category"] == "Food")
    assert food["limit"] == 0.0
    page = client.get("/budget").text
    assert "const hasLimit = item.limit !== null && item.limit !== undefined" in page


def test_financial_totals_sum_authoritative_decimal_amounts(
    client: TestClient,
    web_settings: Settings,
) -> None:
    today = date.today().isoformat()
    with BookmarkDB(web_settings.db_path) as db:
        db.set_app_setting("base_currency", "KWD")
        for index in range(3):
            seed_tx(
                db, tx_id=f"kwd-{index}", tx_date=today, merchant="Corner shop",
                amount=-0.001, category="Food", original_description="SHOP",
                currency="KWD", amount_decimal="-0.001",
            )

    summary = client.get("/api/summary?scope=90d").json()
    assert summary["total_spent"] == 0.003
    assert summary["by_category"]["Food"] == 0.003

    budget = client.get("/api/budget").json()
    food = next(item for item in budget["items"] if item["category"] == "Food")
    assert food["spent"] == 0.003

    trends = client.get("/api/trends").json()
    assert trends["by_year"][str(date.today().year)]["expenses"] == 0.003

    cycle = client.get("/api/summary?scope=cycle").json()
    assert cycle["burn_rate"]["spent_so_far_decimal"] == "0.003"
    assert cycle["burn_rate"]["spent_so_far"] == 0.003
    assert len(cycle["burn_rate"]["daily_spend_decimal"].partition(".")[2]) == 3


def test_budget_limits_keep_their_currency_when_base_changes(
    client: TestClient,
    web_settings: Settings,
) -> None:
    with BookmarkDB(web_settings.db_path) as db:
        db.set_app_setting("base_currency", "KWD")

    saved = client.patch("/api/budget/Food", json={"limit": "1.005"})
    assert saved.status_code == 200
    assert saved.json()["currency"] == "KWD"
    with BookmarkDB(web_settings.db_path) as db:
        assert str(db.get_budget_limits_exact("KWD")["Food"]) == "1.005"
        db.set_app_setting("base_currency", "USD")

    budget = client.get("/api/budget").json()
    assert budget["base_currency"] == "USD"
    assert budget["non_base_budget_count"] == 1
    assert all(item["limit"] is None for item in budget["items"])


def test_api_settings_exposes_setup_status_for_empty_database(
    client: TestClient,
) -> None:
    response = client.get("/api/settings")
    assert response.status_code == 200
    payload = response.json()
    assert payload["is_first_run"] is True
    assert payload["requires_base_currency_setup"] is True
    assert payload["provider"] == "local"
    assert payload["provider_ready"] is True
    assert payload["has_existing_transactions"] is False
    assert payload["google_sheets_configured"] is False
    assert payload["setup_status"]["steps"][0]["id"] == "base_currency"


def test_api_settings_exposes_setup_status_for_populated_database(
    client: TestClient,
    web_settings: Settings,
) -> None:
    with BookmarkDB(web_settings.db_path) as db:
        db.set_app_setting("base_currency", "EUR")
        seed_tx(
            db,
            tx_id="tx-existing",
            tx_date="2026-03-10",
            merchant="Netflix",
            amount=-12.99,
            category="Digital Subscriptions",
            original_description="NETFLIX.COM",
        )

    response = client.get("/api/settings")
    assert response.status_code == 200
    payload = response.json()
    assert payload["is_first_run"] is False
    assert payload["requires_base_currency_setup"] is False
    assert payload["has_existing_transactions"] is True


def test_upload_is_blocked_until_base_currency_is_saved(client: TestClient) -> None:
    response = client.post(
        "/api/upload",
        files={"file": ("test.csv", b"Date,Description,Amount\n2026-02-22,STARBUCKS,-4.50\n", "text/csv")},
    )
    assert response.status_code == 400
    assert response.json()["requires_base_currency_setup"] is True
    assert "save your base currency before importing" in response.json()["error"]


def test_confirm_is_blocked_until_base_currency_is_saved(client: TestClient) -> None:
    response = client.post(
        "/api/confirm",
        json={"transactions": [{"id": "tx-1", "date": "2026-02-22", "merchant": "STARBUCKS", "amount": -4.5}]},
    )
    assert response.status_code == 400
    payload = response.json()
    assert payload["ok"] is False
    assert payload["requires_base_currency_setup"] is True
    assert payload["error"] == payload["message"]


def test_upload_proceeds_after_first_run_setup(client: TestClient) -> None:
    save_response = client.patch("/api/settings/preferences", json={"base_currency": "EUR"})
    assert save_response.status_code == 200

    response = client.post(
        "/api/upload",
        files={"file": ("test.csv", b"Date,Description,Amount\n2026-02-22,STARBUCKS,-4.50\n", "text/csv")},
    )
    assert response.status_code == 200

    events = parse_sse_events(response.text)
    assert events[-1]["done"] is True
    assert events[-1]["transactions"]


def test_upload_parser_runs_in_worker_and_releases_slot_after_failure(
    client: TestClient,
    web_settings: Settings,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import threading

    from spectra import import_service

    client.patch("/api/settings/preferences", json={"base_currency": "EUR"})
    caller_thread = threading.get_ident()
    worker_threads: list[int] = []

    def fail_prepare(*_args, **_kwargs):
        worker_threads.append(threading.get_ident())
        raise RuntimeError("synthetic parser failure")

    monkeypatch.setattr(import_service, "prepare_import", fail_prepare)
    response = client.post(
        "/api/upload",
        files={"file": ("test.csv", b"Date,Description,Amount\n2026-02-22,SHOP,-1.00\n", "text/csv")},
    )

    assert response.status_code == 200
    assert parse_sse_events(response.text)[-1]["error"] is True
    assert worker_threads and worker_threads[0] != caller_thread
    acquired = [server._UPLOAD_IMPORT_SLOTS.acquire(blocking=False) for _ in range(3)]
    assert acquired == [True, True, False]
    for did_acquire in acquired:
        if did_acquire:
            server._UPLOAD_IMPORT_SLOTS.release()


def test_cancelled_upload_keeps_worker_slot_until_cleanup(
    web_settings: Settings,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import asyncio
    import io
    import threading

    from fastapi import UploadFile
    from spectra.services.importer import app as importer_service
    from spectra.import_service import ImportPreparation

    worker_started = threading.Event()
    allow_worker_to_finish = threading.Event()

    def slow_prepare(*_args, **_kwargs):
        worker_started.set()
        allow_worker_to_finish.wait(timeout=3)
        return ImportPreparation([], [], 0, [], "synthetic-batch")

    monkeypatch.setattr(importer_service, "load_settings", lambda: web_settings)
    monkeypatch.setattr(importer_service, "_prepare", slow_prepare)

    async def exercise_cancelled_stream() -> bool:
        upload = UploadFile(
            filename="statement.csv",
            file=io.BytesIO(b"Date,Description,Amount\n2026-02-22,SHOP,-1.00\n"),
        )
        response = await importer_service.create_review_job(upload, "EUR")
        iterator = response.body_iterator
        await iterator.__anext__()
        assert await asyncio.to_thread(worker_started.wait, 2)

        blocked_next_event = asyncio.create_task(iterator.__anext__())
        await asyncio.sleep(0)
        blocked_next_event.cancel()
        with pytest.raises(asyncio.CancelledError):
            await blocked_next_event
        await iterator.aclose()
        await response.background()

        # One slot remains held by the live worker; only the other is available.
        first = importer_service._IMPORT_SLOTS.acquire(blocking=False)
        second = importer_service._IMPORT_SLOTS.acquire(blocking=False)
        assert first is True
        assert second is False
        if first:
            importer_service._IMPORT_SLOTS.release()
        allow_worker_to_finish.set()

        for _ in range(200):
            first = importer_service._IMPORT_SLOTS.acquire(blocking=False)
            second = importer_service._IMPORT_SLOTS.acquire(blocking=False)
            if first and second:
                importer_service._IMPORT_SLOTS.release()
                importer_service._IMPORT_SLOTS.release()
                return True
            if first:
                importer_service._IMPORT_SLOTS.release()
            if second:
                importer_service._IMPORT_SLOTS.release()
            await asyncio.sleep(0.01)
        return False

    assert asyncio.run(exercise_cancelled_stream())


def test_confirm_persistence_runs_in_worker_thread(
    client: TestClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import threading

    caller_thread = threading.get_ident()
    worker_threads: list[int] = []

    def confirm(settings, transactions):
        worker_threads.append(threading.get_ident())
        return {"ok": True, "saved": len(transactions)}

    monkeypatch.setattr(server, "_confirm_import_payload", confirm)
    response = client.post("/api/confirm", json={"transactions": [{"id": "test"}]})

    assert response.status_code == 200
    assert response.json() == {"ok": True, "saved": 1}
    assert worker_threads and worker_threads[0] != caller_thread


def test_confirm_surfaces_importer_conflict_for_review_retry(
    client: TestClient, monkeypatch: pytest.MonkeyPatch,
) -> None:
    from spectra.services.clients import ServiceResponseError

    class LedgerStub:
        def __enter__(self):
            return self

        def __exit__(self, *_exc):
            return None

    class ImportStub:
        def __init__(self, _settings):
            pass

        async def confirm(self, _job_id, _transactions):
            raise ServiceResponseError(
                409,
                "Transaction source field cannot be changed during review: amount",
            )

    monkeypatch.setattr(server, "LedgerClient", lambda _settings: LedgerStub())
    monkeypatch.setattr(
        server, "_build_setup_status",
        lambda _settings, _ledger: {"requires_base_currency_setup": False},
    )
    monkeypatch.setattr(server, "ImportClient", ImportStub)
    response = client.post(
        "/api/confirm",
        json={"job_id": "synthetic-review", "transactions": [{"id": "synthetic-row"}]},
    )

    assert response.status_code == 409
    assert response.json() == {
        "ok": False,
        "message": "Transaction source field cannot be changed during review: amount",
        "retryable": False,
    }


def test_upload_size_limit_rejects_oversized_body(client: TestClient, web_settings: Settings, monkeypatch) -> None:
    monkeypatch.setattr(
        server, "load_settings",
        lambda: web_settings.model_copy(update={"max_upload_bytes": 5}),
    )
    response = client.post(
        "/api/upload",
        files={"file": ("test.csv", b"123456", "text/csv")},
    )
    assert response.status_code == 413


def test_pages_stop_redirecting_after_base_currency_setup(client: TestClient) -> None:
    client.patch("/api/settings/preferences", json={"base_currency": "EUR"})
    response = client.get("/upload", follow_redirects=False)
    assert response.status_code == 200


def test_trends_chart_runtime_is_local_and_dependency_free(client: TestClient) -> None:
    client.patch("/api/settings/preferences", json={"base_currency": "EUR"})
    response = client.get("/trends")
    assert response.status_code == 200
    assert 'id="monthlyChart" class="trend-chart"' in response.text
    assert "new Chart(" not in response.text
    assert "chart.js" not in response.text.lower()

    styles = client.get("/static/styles.css")
    assert styles.status_code == 200
    assert "fonts.googleapis.com" not in styles.text


def test_cycle_mode_can_be_set_to_last_business_day(client: TestClient, web_settings: Settings) -> None:
    response = client.patch(
        "/api/settings/preferences",
        json={"cycle_mode": "last_business_day"},
    )
    assert response.status_code == 200
    payload = response.json()
    assert payload["ok"] is True
    assert payload["cycle_mode"] == "last_business_day"
    assert payload["cycle_rule"] == "last_business_day"
    assert payload["fixed_cycle_start_day"] is None
    assert payload["current_cycle"]["cycle_mode"] == "last_business_day"

    with BookmarkDB(web_settings.db_path) as db:
        assert db.get_app_setting("cycle_start_day") == "last_business_day"


def test_legacy_numeric_cycle_setting_is_clamped_and_exposed_as_fixed_rule(
    client: TestClient,
    web_settings: Settings,
) -> None:
    with BookmarkDB(web_settings.db_path) as db:
        db.set_app_setting("cycle_start_day", "31")

    response = client.get("/api/settings")
    assert response.status_code == 200
    payload = response.json()
    assert payload["cycle_mode"] == "fixed"
    assert payload["fixed_cycle_start_day"] == 28
    assert payload["pay_day"] == 28
    assert payload["cycle_rule"] == "fixed:28"


def test_rule_lifecycle_and_reapply_history(client: TestClient, web_settings: Settings) -> None:
    with BookmarkDB(web_settings.db_path) as db:
        seed_tx(
            db,
            tx_id="tx-rule",
            tx_date="2026-03-09",
            merchant="Amzn Mktp",
            amount=-45.0,
            category="Uncategorized",
            original_description="AMZN MKTP DIGITAL",
        )

    create_response = client.post(
        "/api/settings/rules",
        json={"rule_type": "contains", "pattern": "amzn", "category": "Shopping"},
    )
    assert create_response.status_code == 200
    rule_id = create_response.json()["rule"]["id"]

    test_response = client.post(
        "/api/settings/rules/test",
        json={"rule_type": "contains", "pattern": "amzn", "sample_text": "AMZN MKTP DIGITAL"},
    )
    assert test_response.status_code == 200
    preview = test_response.json()
    assert preview["matches_sample"] is True
    assert preview["impact_count"] >= 1

    disable_response = client.patch(f"/api/settings/rules/{rule_id}", json={"is_active": False})
    assert disable_response.status_code == 200
    assert disable_response.json()["rule"]["is_active"] is False

    enable_response = client.patch(f"/api/settings/rules/{rule_id}", json={"is_active": True})
    assert enable_response.status_code == 200
    assert enable_response.json()["rule"]["is_active"] is True

    reapply_response = client.post("/api/settings/learning/reapply")
    assert reapply_response.status_code == 200
    assert reapply_response.json()["updated"] >= 1

    with BookmarkDB(web_settings.db_path) as db:
        category = db._conn.execute(
            "SELECT category FROM tx_history WHERE tx_id = 'tx-rule'"
        ).fetchone()[0]
        assert category == "Shopping"


def test_summary_and_subscriptions_surface_signals(client: TestClient, web_settings: Settings) -> None:
    today = date.today()
    current_day = today.isoformat()
    prior_cycle_day = (today - timedelta(days=32)).isoformat()
    two_cycles_back_day = (today - timedelta(days=64)).isoformat()

    with BookmarkDB(web_settings.db_path) as db:
        db.save_budget_limit("Food & Dining", 100.0)
        seed_tx(
            db,
            tx_id="tx-food-current",
            tx_date=current_day,
            merchant="Starbucks",
            amount=-80.0,
            category="Food & Dining",
            original_description="POS STARBUCKS",
        )
        seed_tx(
            db,
            tx_id="tx-food-prev",
            tx_date=prior_cycle_day,
            merchant="Starbucks",
            amount=-20.0,
            category="Food & Dining",
            original_description="POS STARBUCKS",
        )
        seed_tx(
            db,
            tx_id="tx-uncat",
            tx_date=current_day,
            merchant="Unknown Merchant",
            amount=-12.0,
            category="Uncategorized",
            original_description="RANDOM UNKNOWN PURCHASE",
        )
        seed_tx(
            db,
            tx_id="tx-uncat-old",
            tx_date=prior_cycle_day,
            merchant="Another Unknown Merchant",
            amount=-8.0,
            category="Uncategorized",
            original_description="ANOTHER UNKNOWN PURCHASE",
        )
        seed_tx(
            db,
            tx_id="sub-old",
            tx_date=two_cycles_back_day,
            merchant="Netflix",
            amount=-9.99,
            category="Digital Subscriptions",
            original_description="NETFLIX.COM",
        )
        seed_tx(
            db,
            tx_id="sub-prev",
            tx_date=prior_cycle_day,
            merchant="Netflix",
            amount=-9.99,
            category="Digital Subscriptions",
            original_description="NETFLIX.COM",
        )
        seed_tx(
            db,
            tx_id="sub-current",
            tx_date=current_day,
            merchant="Netflix",
            amount=-14.99,
            category="Digital Subscriptions",
            original_description="NETFLIX.COM",
        )

    summary_response = client.get("/api/summary?scope=cycle")
    assert summary_response.status_code == 200
    summary = summary_response.json()
    assert summary["uncategorized"] == 1
    assert summary["uncategorized_total"] == 2
    assert isinstance(summary["insights"], list)
    insight_types = {item["type"] for item in summary["insights"]}
    assert "uncategorized" in insight_types

    subscriptions_response = client.get("/api/subscriptions")
    assert subscriptions_response.status_code == 200
    subscriptions = subscriptions_response.json()
    assert subscriptions["summary"]["price_change_count"] >= 1
    netflix = next(item for item in subscriptions["items"] if item["merchant"] == "Netflix")
    assert netflix["price_change_direction"] == "up"
    assert netflix["change_amount"] > 0


def test_confirm_respects_apply_to_future(client: TestClient, web_settings: Settings) -> None:
    with BookmarkDB(web_settings.db_path) as db:
        db.set_app_setting("base_currency", "EUR")

    payload = {
        "transactions": [
            {
                "id": "upload-1",
                "date": "2026-03-11",
                "merchant": "Spotify",
                "category": "Digital Subscriptions",
                "amount": -9.99,
                "currency": "EUR",
                "recurring": "Subscription",
                "original_description": "SPOTIFY AB",
                "apply_to_future": True,
            },
            {
                "id": "upload-2",
                "date": "2026-03-11",
                "merchant": "One-off Store",
                "category": "Shopping",
                "amount": -49.0,
                "currency": "EUR",
                "recurring": "",
                "original_description": "ONE OFF STORE",
                "apply_to_future": False,
            },
        ]
    }

    response = client.post("/api/confirm", json=payload)
    assert response.status_code == 200
    assert response.json()["ok"] is True

    with BookmarkDB(web_settings.db_path) as db:
        assert db._conn.execute("SELECT COUNT(*) FROM tx_history").fetchone()[0] == 2
        merchant_categories = db.get_merchant_categories()
        assert merchant_categories["Spotify"] == "Digital Subscriptions"
        assert "One-off Store" not in merchant_categories

        overrides = db.get_overrides()
        assert overrides["SPOTIFY AB"]["category"] == "Digital Subscriptions"
        assert "ONE OFF STORE" not in overrides

        learning = db.get_recent_learning_feedback(limit=10)
        assert len(learning) >= 2
        assert any(event["clean_name"] == "One-off Store" and event["apply_to_future"] is False for event in learning)


def test_upload_preview_includes_local_review_metadata(
    client: TestClient,
    web_settings: Settings,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    with BookmarkDB(web_settings.db_path) as db:
        db.set_app_setting("base_currency", "EUR")

    import spectra.csv_parser as csv_parser
    import spectra.local_categorizer as local_categorizer
    import spectra.ml_classifier as ml_classifier
    import spectra.recurring as recurring

    monkeypatch.setattr(
        csv_parser,
        "parse_csv",
        lambda _path, currency="EUR": [
            SimpleNamespace(
                id="upload-local-1",
                raw_description="MYSTERY STORE 123",
                amount=-14.2,
                currency=currency,
                date="2026-03-15",
            )
        ],
    )
    monkeypatch.setattr(ml_classifier, "train_classifier", lambda _training_data: object())
    monkeypatch.setattr(
        local_categorizer,
        "categorise_local",
        lambda _rows, merchant_db, ml_classifier=None: [
            CategorisedTransaction(
                id="upload-local-1",
                original_description="MYSTERY STORE 123",
                clean_name="Mystery Store",
                category="Uncategorized",
                amount=-14.2,
                currency="EUR",
                date="2026-03-15",
                classification_source="fallback",
                category_confidence=0.19,
                category_suggestions=[
                    CategorySuggestion(category="Shopping", score=0.19),
                    CategorySuggestion(category="Groceries", score=0.18),
                    CategorySuggestion(category="Food & Dining", score=0.17),
                ],
                needs_review=True,
            )
        ],
    )
    monkeypatch.setattr(recurring, "apply_recurring_tags", lambda _transactions, _history: None)

    response = client.post(
        "/api/upload",
        files={"file": ("sample.csv", b"dummy", "text/csv")},
    )
    assert response.status_code == 200

    events = parse_sse_events(response.text)
    final_event = events[-1]
    preview_row = final_event["transactions"][0]
    assert preview_row["classification_source"] == "fallback"
    assert preview_row["needs_review"] is True
    assert preview_row["category_confidence"] == 0.19
    assert len(preview_row["category_suggestions"]) == 3


def test_upload_preview_omits_local_review_metadata_for_cloud_mode(
    client: TestClient,
    web_settings: Settings,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import asyncio
    cloud_settings = web_settings.model_copy(
        update={
            "ai_provider": "openai",
            "openai_api_key": "test-key",
            "openai_model": "gpt-4o-mini",
        }
    )
    monkeypatch.setattr(server, "load_settings", lambda: cloud_settings)

    with BookmarkDB(web_settings.db_path) as db:
        db.set_app_setting("base_currency", "EUR")

    import spectra.ai as ai_module
    import spectra.csv_parser as csv_parser
    import spectra.recurring as recurring

    monkeypatch.setattr(
        csv_parser,
        "parse_csv",
        lambda _path, currency="EUR": [
            SimpleNamespace(
                id="upload-cloud-1",
                raw_description="SPOTIFY AB",
                amount=-9.99,
                currency=currency,
                date="2026-03-16",
            )
        ],
    )
    monkeypatch.setattr(
        ai_module,
        "categorise",
        lambda _transactions, _existing_categories, **_kwargs: [
            CategorisedTransaction(
                id="upload-cloud-1",
                original_description="SPOTIFY AB",
                clean_name="Spotify",
                category="Digital Subscriptions",
                amount=-9.99,
                currency="EUR",
                date="2026-03-16",
            )
        ],
    )
    monkeypatch.setattr(recurring, "apply_recurring_tags", lambda _transactions, _history: None)

    async def _fast_sleep(_delay: float) -> None:
        return None

    monkeypatch.setattr(asyncio, "sleep", _fast_sleep)

    response = client.post(
        "/api/upload",
        files={"file": ("sample.csv", b"dummy", "text/csv")},
    )
    assert response.status_code == 200

    events = parse_sse_events(response.text)
    final_event = events[-1]
    preview_row = final_event["transactions"][0]
    assert preview_row["classification_source"] == "openai"
    assert preview_row["needs_review"] is False
    assert "category_suggestions" not in preview_row


def test_review_queue_and_transfer_confirmation(client: TestClient, web_settings: Settings) -> None:
    with BookmarkDB(web_settings.db_path) as db:
        seed_tx(
            db,
            tx_id="tx-transfer-1",
            tx_date="2026-03-17",
            merchant="Bank A",
            amount=-42.0,
            category="Transfer",
            original_description="BONIFICO VERSO BANK B",
            transfer_group_id="xfer-1",
            transfer_status="suggested",
            needs_review=True,
            review_reason="transfer_candidate",
        )
        seed_tx(
            db,
            tx_id="tx-transfer-2",
            tx_date="2026-03-17",
            merchant="Bank B",
            amount=42.0,
            category="Transfer In",
            original_description="BONIFICO DA BANK A",
            transfer_group_id="xfer-1",
            transfer_status="suggested",
            needs_review=True,
            review_reason="transfer_candidate",
        )

    tx_export_response = client.get("/api/exports/transactions?transfer_status=suggested")
    assert tx_export_response.status_code == 200
    assert tx_export_response.headers["content-type"].startswith("text/csv")
    assert "tx-transfer-1" in tx_export_response.text
    assert "tx-transfer-2" in tx_export_response.text

    review_export_response = client.get("/api/exports/review?transfer_status=suggested")
    assert review_export_response.status_code == 200
    assert review_export_response.headers["content-type"].startswith("text/csv")
    assert "tx-transfer-1" in review_export_response.text
    assert "tx-transfer-2" in review_export_response.text

    review_response = client.get("/api/review")
    assert review_response.status_code == 200
    review = review_response.json()
    assert review["total"] == 2
    assert any(item["transfer_status"] == "suggested" for item in review["items"])

    confirm_response = client.post(
        "/api/transfers/confirm",
        json={"transfer_group_id": "xfer-1", "action": "confirm"},
    )
    assert confirm_response.status_code == 200
    payload = confirm_response.json()
    assert payload["ok"] is True
    assert len(payload["transactions"]) == 2

    summary_response = client.get("/api/summary?scope=cycle")
    assert summary_response.status_code == 200
    summary = summary_response.json()
    assert summary["confirmed_internal_transfers_count"] >= 1


@pytest.mark.parametrize(
    "path",
    ["/", "/transactions", "/upload", "/review", "/budget", "/trends", "/subscriptions", "/settings"],
)
def test_apple_shell_is_shared_by_every_finance_screen(
    client: TestClient, web_settings: Settings, path: str,
) -> None:
    db = BookmarkDB(web_settings.db_path)
    db.set_app_setting("base_currency", "EUR")
    db.close()
    response = client.get(path)

    assert response.status_code == 200
    assert 'href="/static/apple.css?v=20261011b"' in response.text
    assert "Private on this device" in response.text
    assert 'aria-label="Main navigation"' in response.text
    assert "cdn." not in response.text


def test_apple_stylesheet_is_served_as_a_local_asset(client: TestClient) -> None:
    response = client.get("/static/apple.css")

    assert response.status_code == 200
    assert "--accent: #0066cc" in response.text
    assert "prefers-reduced-motion: reduce" in response.text
    assert "text-decoration: none !important" in response.text
    assert "--text-faint: #6e6e73" in response.text


def test_upload_drop_zone_is_keyboard_accessible(
    client: TestClient, web_settings: Settings,
) -> None:
    db = BookmarkDB(web_settings.db_path)
    db.set_app_setting("base_currency", "EUR")
    db.close()
    response = client.get("/upload")

    assert 'role="button"' in response.text
    assert 'tabindex="0"' in response.text
    assert 'aria-label="Choose a bank export file"' in response.text
    assert "e.key === 'Enter' || e.key === ' '" in response.text


def test_upload_confirmation_template_keeps_failed_review_retryable(
    client: TestClient, web_settings: Settings,
) -> None:
    db = BookmarkDB(web_settings.db_path)
    db.set_app_setting("base_currency", "EUR")
    db.close()
    response = client.get("/upload")

    assert response.status_code == 200
    assert 'id="confirmError" role="alert"' in response.text
    script = response.text.split("async function confirmImport() {", maxsplit=1)[1]
    confirmation = script.split("\n    }\n\n    loadPreviewCategories();", maxsplit=1)[0]
    catch_at = confirmation.index("} catch (err) {")
    error_path = confirmation[confirmation.index("if (!res.ok"):catch_at]
    catch_path = confirmation[catch_at:]

    assert "if (!res.ok || data.ok === false || data.error)" in error_path
    assert error_path.index("throw new Error(message)") < error_path.index("window.location.href")
    assert "errorNotice.textContent = message" in catch_path
    assert "errorNotice.style.display = ''" in catch_path
    assert "btn.disabled = false" in catch_path
    assert "btn.textContent = 'Confirm & Save'" in catch_path
    assert "previewJobId = null" not in confirmation
    assert "previewData = []" not in confirmation
