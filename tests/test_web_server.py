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
        log_level="DEBUG",
    )


@pytest.fixture
def client(monkeypatch: pytest.MonkeyPatch, web_settings: Settings) -> TestClient:
    monkeypatch.setattr(server, "load_settings", lambda: web_settings)
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
