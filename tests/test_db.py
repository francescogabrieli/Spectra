"""Unit tests for the bookmark database."""

from pathlib import Path
import sqlite3

import pytest

from spectra.ai import CategorisedTransaction
from spectra.db import BookmarkDB


@pytest.fixture
def db(tmp_path: Path) -> BookmarkDB:
    return BookmarkDB(tmp_path / "test.db")


class TestSeenTransactions:
    """Transaction deduplication tests."""

    def test_unseen_returns_false(self, db: BookmarkDB) -> None:
        assert db.is_seen("tx-001") is False

    def test_mark_seen_then_is_seen(self, db: BookmarkDB) -> None:
        db.mark_seen("tx-001")
        assert db.is_seen("tx-001") is True

    def test_mark_seen_idempotent(self, db: BookmarkDB) -> None:
        db.mark_seen("tx-001")
        db.mark_seen("tx-001")  # Should not raise
        assert db.is_seen("tx-001") is True

    def test_mark_seen_batch(self, db: BookmarkDB) -> None:
        db.mark_seen_batch(["tx-001", "tx-002", "tx-003"])
        assert db.is_seen("tx-001") is True
        assert db.is_seen("tx-002") is True
        assert db.is_seen("tx-003") is True
        assert db.is_seen("tx-999") is False

    def test_count(self, db: BookmarkDB) -> None:
        assert db.count() == 0
        db.mark_seen_batch(["a", "b", "c"])
        assert db.count() == 3


class TestOverrides:
    """LLM Feedback Loop override tests."""
    
    def test_save_and_get_overrides(self, db: BookmarkDB) -> None:
        overrides = {
            "AMZ AWS EMEA": {"clean_name": "AWS", "category": "Cloud Computing"},
            "UBER TRIP SAN FRAN": {"clean_name": "Uber", "category": "Transport"},
        }
        
        db.save_overrides(overrides)
        fetched = db.get_overrides()
        
        assert len(fetched) == 2
        assert "AMZ AWS EMEA" in fetched
        assert fetched["AMZ AWS EMEA"]["clean_name"] == "AWS"
        assert fetched["UBER TRIP SAN FRAN"]["category"] == "Transport"
        
    def test_save_overrides_upsert(self, db: BookmarkDB) -> None:
        db.save_overrides({"MCDONALDS STR": {"clean_name": "McDonalds", "category": "Food"}})
        assert db.get_overrides()["MCDONALDS STR"]["category"] == "Food"
        
        # Upsert should overwrite old category
        db.save_overrides({"MCDONALDS STR": {"clean_name": "McDonalds", "category": "Fast Food"}})
        fetched = db.get_overrides()
        
        assert len(fetched) == 1
        assert fetched["MCDONALDS STR"]["category"] == "Fast Food"


class TestContextManager:
    def test_context_manager(self, tmp_path: Path) -> None:
        with BookmarkDB(tmp_path / "ctx.db") as db:
            db.mark_seen("tx-001")
            assert db.is_seen("tx-001") is True


class TestResetDatabase:
    def test_reset_all_data_clears_tables(self, db: BookmarkDB) -> None:
        db.mark_seen_batch(["tx-1", "tx-2"])
        db.save_merchant_category("netflix", "Digital Subscriptions")
        db.save_overrides({
            "NETFLIX.COM": {"clean_name": "netflix", "category": "Digital Subscriptions"}
        })
        db.save_budget_limit("Digital Subscriptions", 100)

        deleted = db.reset_all_data()

        assert deleted["seen_transactions"] >= 2
        assert deleted["merchant_categories"] >= 1
        assert deleted["user_overrides"] >= 1
        assert deleted["budget_limits"] >= 1
        assert deleted["tx_history"] == 0

        assert db.count() == 0
        assert db.get_merchant_categories() == {}
        assert db.get_overrides() == {}
        assert db.get_budget_limits() == {}


def test_legacy_history_currency_remains_unknown_until_explicit_confirmation(
    tmp_path: Path,
) -> None:
    path = tmp_path / "legacy.db"
    conn = sqlite3.connect(path)
    conn.execute(
        """CREATE TABLE tx_history (
            tx_id TEXT PRIMARY KEY, date TEXT NOT NULL, clean_name TEXT NOT NULL,
            amount REAL NOT NULL, category TEXT NOT NULL DEFAULT 'Uncategorized',
            original_description TEXT NOT NULL DEFAULT ''
        )"""
    )
    conn.execute(
        "INSERT INTO tx_history (tx_id, date, clean_name, amount, category, original_description) VALUES (?, ?, ?, ?, ?, ?)",
        ("legacy-1", "2025-04-10", "Market", -12.0, "Food", "MARKET"),
    )
    conn.commit()
    conn.close()

    with BookmarkDB(path) as db:
        db.set_app_setting("base_currency", "USD")
        transaction = db.get_transaction("legacy-1")
        assert transaction is not None
        assert transaction["amount"] == pytest.approx(-12.0)
        assert transaction["currency"] is None
        assert transaction["currency_source"] == "legacy-unknown"
        assert transaction["needs_review"] is True
        assert db.list_legacy_unknown_currency()["total"] == 1

        assert db.confirm_legacy_currencies([{"id": "legacy-1", "currency": "JPY"}]) == 1
        resolved = db.get_transaction("legacy-1")
        assert resolved is not None
        assert resolved["amount"] == pytest.approx(-12.0)
        assert resolved["currency"] == "JPY"
        assert resolved["amount_decimal"] == "-12"
        assert resolved["currency_source"] == "user-confirmed"
        assert resolved["needs_review"] is False
        assert db.list_legacy_unknown_currency()["total"] == 0


def test_legacy_currency_confirmation_is_atomic_and_rejects_already_resolved_rows(
    tmp_path: Path,
) -> None:
    path = tmp_path / "legacy-atomic.db"
    conn = sqlite3.connect(path)
    conn.execute(
        """CREATE TABLE tx_history (
            tx_id TEXT PRIMARY KEY, date TEXT NOT NULL, clean_name TEXT NOT NULL,
            amount REAL NOT NULL, category TEXT NOT NULL DEFAULT 'Uncategorized',
            original_description TEXT NOT NULL DEFAULT ''
        )"""
    )
    conn.executemany(
        "INSERT INTO tx_history (tx_id, date, clean_name, amount) VALUES (?, ?, ?, ?)",
        [("legacy-a", "2025-04-10", "A", -1.0), ("legacy-b", "2025-04-11", "B", -2.0)],
    )
    conn.commit()
    conn.close()

    with BookmarkDB(path) as db:
        with pytest.raises(ValueError, match="no longer awaiting"):
            db.confirm_legacy_currencies(
                [{"id": "legacy-a", "currency": "EUR"}, {"id": "missing", "currency": "GBP"}]
            )
        assert db.get_transaction("legacy-a")["currency"] is None
        assert db.get_transaction("legacy-b")["currency"] is None


def test_legacy_currency_confirmation_requires_explicit_minor_unit_rounding(
    tmp_path: Path,
) -> None:
    path = tmp_path / "legacy-precision.db"
    conn = sqlite3.connect(path)
    conn.execute(
        """CREATE TABLE tx_history (
            tx_id TEXT PRIMARY KEY, date TEXT NOT NULL, clean_name TEXT NOT NULL,
            amount REAL NOT NULL, category TEXT NOT NULL DEFAULT 'Uncategorized',
            original_description TEXT NOT NULL DEFAULT ''
        )"""
    )
    conn.execute(
        "INSERT INTO tx_history (tx_id, date, clean_name, amount) VALUES (?, ?, ?, ?)",
        ("legacy-precision", "2025-04-10", "Market", -12.345),
    )
    conn.commit()
    conn.close()

    with BookmarkDB(path) as db:
        with pytest.raises(ValueError, match="provide amount_decimal"):
            db.confirm_legacy_currencies([{"id": "legacy-precision", "currency": "JPY"}])
        assert db.get_transaction("legacy-precision")["currency"] is None
        with pytest.raises(ValueError, match="precision not supported"):
            db.confirm_legacy_currencies(
                [{"id": "legacy-precision", "currency": "JPY", "amount_decimal": "-12.3"}]
            )
        assert db.get_transaction("legacy-precision")["currency"] is None
        db.confirm_legacy_currencies(
            [{"id": "legacy-precision", "currency": "JPY", "amount_decimal": "-12"}]
        )
        tx = db.get_transaction("legacy-precision")
        assert tx["currency"] == "JPY"
        assert tx["amount"] == -12.0
        assert tx["amount_minor"] == -12


def test_legacy_budget_migration_does_not_guess_its_currency(tmp_path: Path) -> None:
    path = tmp_path / "legacy-budget.db"
    conn = sqlite3.connect(path)
    conn.execute(
        "CREATE TABLE budget_limits (category TEXT PRIMARY KEY, monthly_limit REAL NOT NULL)"
    )
    conn.execute("INSERT INTO budget_limits VALUES ('Food', 125.50)")
    conn.commit()
    conn.close()

    with BookmarkDB(path) as db:
        assert db.get_budget_limits() == {"Food": 125.5}
        assert db.get_budget_limits("EUR") == {}
        assert db.count_non_base_budget_limits("EUR") == 1


def test_import_transaction_and_learning_write_roll_back_together(db: BookmarkDB) -> None:
    tx = CategorisedTransaction(
        id="atomic-tx", date="2026-04-10", clean_name="Shop", original_description="SHOP",
        amount=-2.0, amount_decimal="-2.00", currency="EUR", category="Food",
    )
    db._conn.execute(
        """CREATE TRIGGER reject_learning BEFORE INSERT ON learning_feedback
           BEGIN SELECT RAISE(ABORT, 'test rollback'); END"""
    )

    with pytest.raises(sqlite3.IntegrityError, match="test rollback"):
        db.save_import_atomic(
            [tx],
            learning_feedback=[{
                "tx_id": "atomic-tx", "clean_name": "Shop", "category": "Food",
                "source": "test",
            }],
        )

    assert db.get_transaction("atomic-tx") is None
    assert db.is_seen("atomic-tx") is False
