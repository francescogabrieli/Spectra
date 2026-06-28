"""Tests for runtime configuration and legacy compatibility."""

from pathlib import Path

from spectra.config import Settings
from spectra.db import BookmarkDB


def _creds_file(tmp_path: Path) -> Path:
    creds = tmp_path / "credentials.json"
    creds.write_text("{}")
    return creds


def test_legacy_ai_provider_alias_is_still_supported(
    monkeypatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setenv("AI_PrOVIDER", "LOCAL")
    settings = Settings(
        google_sheets_credentials_file=str(_creds_file(tmp_path)),
        db_path=tmp_path / "spectra.db",
    )
    assert settings.ai_provider == "local"


def test_default_db_name_is_spectra(tmp_path: Path) -> None:
    settings = Settings(
        google_sheets_credentials_file=str(_creds_file(tmp_path)),
        db_path=tmp_path / "data" / "spectra.db",
    )
    assert settings.db_path.name == "spectra.db"


def test_legacy_prism_db_is_migrated_to_spectra_db(tmp_path: Path) -> None:
    data_dir = tmp_path / "data"
    legacy_db_path = data_dir / "prism.db"
    with BookmarkDB(legacy_db_path) as db:
        db.mark_seen("tx-legacy")

    settings = Settings(
        google_sheets_credentials_file=str(_creds_file(tmp_path)),
        db_path=data_dir / "spectra.db",
    )

    assert settings.db_path == data_dir / "spectra.db"
    assert settings.db_path.exists()

    with BookmarkDB(settings.db_path) as db:
        assert db.is_seen("tx-legacy") is True


def test_spectra_db_wins_when_both_files_exist(tmp_path: Path) -> None:
    data_dir = tmp_path / "data"
    legacy_db_path = data_dir / "prism.db"
    current_db_path = data_dir / "spectra.db"

    with BookmarkDB(legacy_db_path) as legacy:
        legacy.mark_seen("tx-legacy")
    with BookmarkDB(current_db_path) as current:
        current.mark_seen("tx-current")

    settings = Settings(
        google_sheets_credentials_file=str(_creds_file(tmp_path)),
        db_path=current_db_path,
    )

    with BookmarkDB(settings.db_path) as db:
        assert db.is_seen("tx-current") is True
        assert db.is_seen("tx-legacy") is False
