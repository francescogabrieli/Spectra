"""Native launcher process boundaries and secret ownership."""

from pathlib import Path
import re

from spectra.config import Settings
from spectra.launcher import _child_environment
from spectra import pipeline


def test_launcher_passes_optional_credentials_only_to_their_service(
    monkeypatch, tmp_path: Path,
) -> None:
    for name, value in (
        ("OPENAI_API_KEY", "openai-test-secret"),
        ("GEMINI_API_KEY", "gemini-test-secret"),
        ("GOOGLE_SHEETS_CREDENTIALS_B64", "sheets-test-secret"),
    ):
        monkeypatch.setenv(name, value)

    settings = Settings(
        service_role="launcher",
        ai_provider="openai",
        openai_api_key="openai-test-secret",
        gemini_api_key="gemini-test-secret",
        google_sheets_credentials_b64="sheets-test-secret",
        spreadsheet_id="synthetic-sheet",
        google_sheets_credentials_file=str(tmp_path / "synthetic-credentials.json"),
        db_path=tmp_path / "finance" / "spectra.db",
        import_db_path=tmp_path / "staging.sqlite",
        fx_db_path=tmp_path / "rates.sqlite",
    )
    ports = {"ledger": 18081, "importer": 18082, "enrichment": 18083}

    gateway = _child_environment(settings, "gateway", ports)
    ledger = _child_environment(settings, "ledger", ports)
    importer = _child_environment(settings, "importer", ports)
    enrichment = _child_environment(settings, "enrichment", ports)

    for environment in (gateway, ledger):
        assert "OPENAI_API_KEY" not in environment
        assert "GEMINI_API_KEY" not in environment
        assert "SPREADSHEET_ID" not in environment
        assert "GOOGLE_SHEETS_CREDENTIALS_B64" not in environment
    assert enrichment["OPENAI_API_KEY"] == "openai-test-secret"
    assert enrichment["GEMINI_API_KEY"] == "gemini-test-secret"
    assert "GOOGLE_SHEETS_CREDENTIALS_B64" not in enrichment
    assert "SPREADSHEET_ID" not in enrichment
    for environment in (gateway, ledger, enrichment):
        assert "GOOGLE_SHEETS_CREDENTIALS_FILE" not in environment
    assert importer["SPREADSHEET_ID"] == "synthetic-sheet"
    assert "OPENAI_API_KEY" not in importer
    assert "GEMINI_API_KEY" not in importer
    assert importer["GOOGLE_SHEETS_CREDENTIALS_FILE"] == str(
        tmp_path / "synthetic-credentials.json"
    )


def test_compose_and_build_context_keep_optional_secrets_with_their_owner() -> None:
    root = Path(__file__).resolve().parents[1]
    compose = (root / "docker-compose.yml").read_text(encoding="utf-8")
    dockerignore = (root / ".dockerignore").read_text(encoding="utf-8").splitlines()
    sections = re.split(r"(?=\n  (?:gateway|importer|ledger|enrichment):)", compose)
    service_sections = {
        match.group(1): section
        for section in sections
        if (match := re.match(r"\n  (gateway|importer|ledger|enrichment):", section))
    }

    assert "OPENAI_API_KEY" not in service_sections["gateway"]
    assert "GEMINI_API_KEY" not in service_sections["gateway"]
    assert "GOOGLE_SHEETS_CREDENTIALS_B64" not in service_sections["gateway"]
    assert "OPENAI_API_KEY" not in service_sections["importer"]
    assert "GOOGLE_SHEETS_CREDENTIALS_B64" in service_sections["importer"]
    assert "OPENAI_API_KEY" not in service_sections["ledger"]
    assert "GOOGLE_SHEETS_CREDENTIALS_B64" not in service_sections["ledger"]
    assert "OPENAI_API_KEY" in service_sections["enrichment"]
    assert "GOOGLE_SHEETS_CREDENTIALS_B64" not in service_sections["enrichment"]
    assert "ports:" in service_sections["gateway"]
    for service in ("importer", "ledger", "enrichment"):
        assert "ports:" not in service_sections[service]
    assert "127.0.0.1" in service_sections["gateway"]
    assert "./data/import-staging:/app/staging" in service_sections["importer"]
    assert "./data:/app/data" in service_sections["ledger"]
    assert "./data/enrichment-cache:/app/fx-cache" in service_sections["enrichment"]

    for pattern in (
        ".env", ".env.*", "credentials.json", "**/*credentials*.json", "*.pem", "*.key",
    ):
        assert pattern in dockerignore


def test_cli_serve_uses_launcher_role_without_financial_store_migration(monkeypatch) -> None:
    loaded_roles: list[str] = []
    launched: list[tuple[Settings, str, int]] = []

    def fake_load_settings(*, service_role: str):
        loaded_roles.append(service_role)
        return Settings(service_role=service_role)

    def fake_launch(settings: Settings, *, host: str, port: int) -> None:
        launched.append((settings, host, port))

    monkeypatch.setattr(pipeline, "load_settings", fake_load_settings)
    monkeypatch.setattr("spectra.launcher.launch_local_services", fake_launch)
    monkeypatch.setattr("sys.argv", ["spectra", "--serve", "--host", "127.0.0.1", "--port", "8099"])

    pipeline.main()

    assert loaded_roles == ["launcher"]
    assert launched[0][0].service_role == "launcher"
    assert launched[0][1:] == ("127.0.0.1", 8099)
