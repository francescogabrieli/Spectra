from __future__ import annotations

from pathlib import Path
from spectra.ai import CategorisedTransaction
from spectra.config import Settings
from spectra.csv_parser import ParsedTransaction, parse_csv
from spectra.db import BookmarkDB
from spectra.import_service import prepare_import


def _settings(path: Path) -> Settings:
    return Settings(ai_provider="local", db_path=path, base_currency="EUR")


def test_overlapping_and_reimported_statements_are_idempotent(tmp_path: Path) -> None:
    full = tmp_path / "full.csv"
    overlap = tmp_path / "overlap.csv"
    header = "Date,Description,Amount,Currency,Account\n"
    repeated = "2026-02-22,MARKET,-10.00,EUR,Checking A\n"
    full.write_text(
        header + repeated + repeated
        + "2026-02-22,MARKET,-10.00,EUR,Checking B\n"
        + "2026-02-22,MARKET,-10.00,USD,Checking A\n"
    )
    overlap.write_text(header + repeated + repeated)

    first_import = parse_csv(full)
    overlapping_rows = parse_csv(overlap)
    assert len({row.id for row in first_import}) == 4
    assert {row.id for row in overlapping_rows}.issubset({row.id for row in first_import})

    saved = [
        CategorisedTransaction(
            id=row.id, original_description=row.raw_description,
            clean_name=row.counterpart or row.raw_description, category="Food",
            amount=row.amount, amount_decimal=row.amount_decimal,
            currency=row.currency, date=row.date, account_name=row.account_name,
            account_id=row.account_id,
        )
        for row in first_import
    ]
    with BookmarkDB(tmp_path / "imports.db") as db:
        db.save_history(saved)
        full_retry = prepare_import(first_import, settings=_settings(tmp_path / "imports.db"), db=db)
        overlap_retry = prepare_import(overlapping_rows, settings=_settings(tmp_path / "imports.db"), db=db)
        assert full_retry.transactions == []
        assert full_retry.skipped_duplicates == 4
        assert overlap_retry.transactions == []
        assert overlap_retry.skipped_duplicates == 2
        assert db._conn.execute("SELECT COUNT(*) FROM tx_history").fetchone()[0] == 4


def test_new_scoped_id_resolves_to_legacy_import_marker(tmp_path: Path) -> None:
    statement = tmp_path / "scoped.csv"
    statement.write_text(
        "Date,Description,Amount,Currency,Account\n"
        "2026-02-22,MARKET,-10.00,EUR,Checking A\n"
    )
    source = parse_csv(statement)[0]
    legacy = CategorisedTransaction(
        id=source.legacy_id, original_description=source.raw_description,
        clean_name="Market", category="Food", amount=source.amount,
        currency="EUR", date=source.date, account_name="",
    )

    with BookmarkDB(tmp_path / "legacy.db") as db:
        db.save_history([legacy])
        retry = prepare_import([source], settings=_settings(tmp_path / "legacy.db"), db=db)
        assert retry.transactions == []
        assert retry.skipped_duplicates == 1
        assert db._conn.execute("SELECT COUNT(*) FROM tx_history").fetchone()[0] == 1


def test_fx_failure_preserves_source_currency_and_amount(tmp_path: Path, monkeypatch) -> None:
    import spectra.fx

    source = ParsedTransaction(
        id="usd-source", date="2026-02-22", amount=-100.25, currency="USD",
        raw_description="INTERNATIONAL PURCHASE", amount_decimal="-100.25",
    )

    def fail_offline(*_args, **_kwargs):
        raise spectra.fx.FXConversionError("offline")

    monkeypatch.setattr(spectra.fx, "convert_currency_result", fail_offline)
    with BookmarkDB(tmp_path / "fx.db") as db:
        db.set_app_setting("base_currency", "EUR")
        db.save_overrides({
            source.raw_description: {"clean_name": "Merchant", "category": "Shopping"}
        })
        prepared = prepare_import([source], settings=_settings(tmp_path / "fx.db"), db=db)

    tx = prepared.transactions[0]
    assert tx.amount == -100.25
    assert tx.amount_decimal == "-100.25"
    assert tx.currency == "USD"
    assert tx.original_amount == -100.25
    assert tx.original_currency == "USD"
    assert tx.conversion_error == "offline"
    assert tx.needs_review is True
    assert tx.review_reason == "currency_conversion_failed"
