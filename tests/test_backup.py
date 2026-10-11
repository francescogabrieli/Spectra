from __future__ import annotations

import os

import pytest

from spectra.backup import create_backup, restore_backup
from spectra.db import BookmarkDB


def test_backup_and_restore_preserve_database_without_overwriting(tmp_path) -> None:
    source = tmp_path / "spectra.db"
    backup = tmp_path / "backup" / "spectra-snapshot.db"
    restored = tmp_path / "restored" / "spectra.db"
    with BookmarkDB(source) as db:
        db.set_app_setting("base_currency", "EUR")
        db._conn.execute(
            """INSERT INTO tx_history
               (tx_id, date, clean_name, amount, amount_currency, amount_decimal,
                category, original_description)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
            ("synthetic", "2026-02-22", "Market", -1.25, "EUR", "-1.25", "Food", "MARKET"),
        )
        db._conn.commit()

    assert create_backup(source, backup) == backup
    assert os.stat(backup).st_mode & 0o777 == 0o600
    assert restore_backup(backup, restored) == restored
    with BookmarkDB(restored) as db:
        row = db.get_transaction("synthetic")
        assert row is not None
        assert row["currency"] == "EUR"
        assert row["amount_decimal"] == "-1.25"
        assert db.get_app_setting("base_currency") == "EUR"

    with pytest.raises(FileExistsError):
        restore_backup(backup, restored)


def test_restore_rejects_invalid_or_non_spectra_sqlite_database(tmp_path) -> None:
    invalid = tmp_path / "not-spectra.db"
    import sqlite3

    with sqlite3.connect(invalid) as connection:
        connection.execute("CREATE TABLE unrelated (value TEXT)")

    with pytest.raises(ValueError, match="not a Spectra"):
        restore_backup(invalid, tmp_path / "restored.db")
    assert not (tmp_path / "restored.db").exists()
