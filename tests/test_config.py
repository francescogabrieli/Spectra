"""Tests for runtime configuration and legacy compatibility."""

from pathlib import Path

import pytest

from spectra.config import Settings, load_settings
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
        service_role="ledger",
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
        service_role="ledger",
    )

    with BookmarkDB(settings.db_path) as db:
        assert db.is_seen("tx-current") is True
        assert db.is_seen("tx-legacy") is False


@pytest.mark.parametrize("service_role", ["gateway", "importer", "enrichment"])
def test_non_ledger_configuration_never_moves_the_legacy_financial_store(
    service_role: str,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    data_dir = tmp_path / "finance"
    legacy_path = data_dir / "prism.db"
    with BookmarkDB(legacy_path) as db:
        db.mark_seen("synthetic-legacy-row")

    monkeypatch.setenv("DB_PATH", str(data_dir / "spectra.db"))
    monkeypatch.setenv("IMPORT_DB_PATH", str(tmp_path / "staging.sqlite"))
    monkeypatch.setenv("FX_DB_PATH", str(tmp_path / "rates.sqlite"))
    monkeypatch.setenv("SPECTRA_SERVICE_ROLE", service_role)
    settings = load_settings(service_role=service_role)

    assert settings.db_path == data_dir / "spectra.db"
    assert legacy_path.is_file()
    assert not settings.db_path.exists()


@pytest.mark.parametrize(
    ("service_role", "expected_openai", "expected_sheets"),
    [
        ("gateway", "", ""),
        ("ledger", "", ""),
        ("importer", "", "synthetic-sheets-secret"),
        ("enrichment", "synthetic-openai-secret", ""),
    ],
)
def test_service_settings_confine_dotenv_credentials(
    service_role: str,
    expected_openai: str,
    expected_sheets: str,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    env_file = tmp_path / "synthetic.env"
    env_file.write_text(
        "\n".join(
            (
                "AI_PROVIDER=openai",
                "OPENAI_API_KEY=synthetic-openai-secret",
                "GEMINI_API_KEY=synthetic-gemini-secret",
                "SPREADSHEET_ID=synthetic-sheet-id",
                "GOOGLE_SHEETS_CREDENTIALS_B64=synthetic-sheets-secret",
                f"GOOGLE_SHEETS_CREDENTIALS_FILE={tmp_path / 'synthetic-service-account.json'}",
            )
        ),
        encoding="utf-8",
    )
    for name in (
        "AI_PROVIDER", "OPENAI_API_KEY", "GEMINI_API_KEY", "SPREADSHEET_ID",
        "GOOGLE_SHEETS_CREDENTIALS_B64", "GOOGLE_SHEETS_CREDENTIALS_FILE",
    ):
        monkeypatch.delenv(name, raising=False)

    settings = load_settings(service_role=service_role, env_file=env_file)

    assert settings.openai_api_key == expected_openai
    assert settings.gemini_api_key == (
        "synthetic-gemini-secret" if service_role == "enrichment" else ""
    )
    assert settings.google_sheets_credentials_b64 == expected_sheets
    assert settings.spreadsheet_id == ("synthetic-sheet-id" if service_role == "importer" else "")
    if service_role != "importer":
        assert settings.google_sheets_credentials_file.endswith("/credentials.json")
