"""CLI adapter tests; actual API commits are covered by service integration tests."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock

import pytest

from spectra.config import Settings
from spectra.pipeline import run


@pytest.fixture
def settings(tmp_path: Path) -> Settings:
    return Settings(
        service_role="gateway",
        ai_provider="local",
        db_path=tmp_path / "runtime" / "spectra.db",
        import_db_path=tmp_path / "runtime" / "imports.sqlite",
        fx_db_path=tmp_path / "runtime" / "rates.sqlite",
    )


@pytest.fixture
def csv_file(tmp_path: Path) -> str:
    path = tmp_path / "synthetic.csv"
    path.write_text(
        "Data Operazione;Descrizione;Importo;Valuta\n"
        "22/02/2026;POS STARBUCKS;-4,50;EUR\n",
        encoding="utf-8",
    )
    return str(path)


def test_run_uses_import_api_without_opening_ledger(
    settings: Settings,
    csv_file: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[tuple[str, bytes, str, bool]] = []

    class Client:
        def __init__(self, _settings: Settings) -> None:
            pass

        async def cli_import(self, filename, content, currency, dry_run):
            calls.append((filename, content, currency, dry_run))
            return {"ok": True, "dry_run": False, "transaction_count": 1}

    monkeypatch.setattr("spectra.pipeline.ImportClient", Client)
    run(settings, file=csv_file, currency="EUR", dry_run=False)

    assert calls == [("synthetic.csv", Path(csv_file).read_bytes(), "EUR", False)]
    assert not settings.db_path.exists()


def test_dry_run_writes_only_the_requested_local_preview(
    settings: Settings,
    csv_file: str,
    monkeypatch: pytest.MonkeyPatch,
    isolate_spectra_runtime: MagicMock,
) -> None:
    from spectra.ai import CategorisedTransaction

    row = CategorisedTransaction(
        id="synthetic-1", original_description="POS SHOP", clean_name="Shop",
        category="Food", amount=-1.25, currency="EUR", date="2026-02-22",
    )

    class Client:
        def __init__(self, _settings: Settings) -> None:
            pass

        async def cli_import(self, *_args):
            return {"ok": True, "dry_run": True, "transactions": [row.model_dump(mode="json")]}

    monkeypatch.setattr("spectra.pipeline.ImportClient", Client)
    run(settings, file=csv_file, currency="EUR", dry_run=True)

    report = settings.db_path.parent / "dry_run_report.html"
    assert report.is_file()
    assert not settings.db_path.exists()
    isolate_spectra_runtime.assert_called_once_with(report.as_uri())


def test_run_reports_unavailable_local_services(
    settings: Settings,
    csv_file: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from spectra.services.clients import ServiceUnavailable

    class Client:
        def __init__(self, _settings: Settings) -> None:
            pass

        async def cli_import(self, *_args):
            raise ServiceUnavailable("not running")

    monkeypatch.setattr("spectra.pipeline.ImportClient", Client)
    with pytest.raises(RuntimeError, match="services are not running"):
        run(settings, file=csv_file, currency="EUR", dry_run=False)


def test_cli_uses_saved_base_currency_from_ledger_api(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    import sys

    from spectra import pipeline

    settings = Settings(service_role="gateway", ai_provider="local", db_path=tmp_path / "cli.db")
    run_mock = MagicMock()

    class Ledger:
        def __init__(self, _settings: Settings) -> None:
            pass

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return None

        def get_app_setting(self, _key):
            return "USD"

    monkeypatch.setattr(pipeline, "load_settings", lambda **_kwargs: settings)
    monkeypatch.setattr(pipeline, "LedgerClient", Ledger)
    monkeypatch.setattr(pipeline, "run", run_mock)
    monkeypatch.setattr(sys, "argv", ["spectra", "--file", "statement.csv"])

    pipeline.main()

    run_mock.assert_called_once_with(
        settings, file="statement.csv", currency="USD", dry_run=False,
    )
