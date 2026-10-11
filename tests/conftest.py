"""Keep ordinary tests isolated from a developer's Spectra data and desktop."""

from __future__ import annotations

import webbrowser
from unittest.mock import MagicMock

import pytest


@pytest.fixture(autouse=True)
def isolate_spectra_runtime(monkeypatch: pytest.MonkeyPatch, tmp_path) -> MagicMock:
    """Route default runtime paths to this test's temporary directory."""
    monkeypatch.setenv("DB_PATH", str(tmp_path / "runtime" / "spectra.db"))
    monkeypatch.setenv("IMPORT_DB_PATH", str(tmp_path / "runtime" / "imports.sqlite"))
    monkeypatch.setenv("FX_DB_PATH", str(tmp_path / "runtime" / "fx-rates.sqlite"))
    monkeypatch.setenv("SPECTRA_SERVICE_ROLE", "gateway")
    monkeypatch.setenv("AI_PROVIDER", "local")
    monkeypatch.setenv("AI_PrOVIDER", "local")
    monkeypatch.setenv("OPENAI_API_KEY", "")
    monkeypatch.setenv("GEMINI_API_KEY", "")
    monkeypatch.setenv("SPREADSHEET_ID", "")
    monkeypatch.setenv("GOOGLE_SHEETS_CREDENTIALS_B64", "")
    monkeypatch.setenv(
        "GOOGLE_SHEETS_CREDENTIALS_FILE",
        str(tmp_path / "credentials-not-present.json"),
    )
    browser_open = MagicMock(return_value=False)
    monkeypatch.setattr(webbrowser, "open", browser_open)
    return browser_open
