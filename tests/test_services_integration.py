"""HTTP contract tests for the local four-process service topology."""

from __future__ import annotations

import os
import signal
import socket
import subprocess
import sys
import time
from pathlib import Path
from urllib.error import URLError
from urllib.request import urlopen
import tempfile

import httpx
import pytest

from spectra.config import Settings
from spectra.db import BookmarkDB
from spectra.pipeline import run as run_cli_import
from spectra.services.clients import LedgerClient


ROOT = Path(__file__).resolve().parents[1]


def _unused_port() -> int:
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        return int(listener.getsockname()[1])


class LocalServiceStack:
    def __init__(self, root: Path) -> None:
        self.root = root
        self.processes: dict[str, subprocess.Popen] = {}
        self.ports = {name: _unused_port() for name in ("gateway", "ledger", "importer", "enrichment")}
        self.paths = {
            "db": root / "ledger" / "spectra.db",
            "imports": root / "staging" / "imports.sqlite",
            "fx": root / "fx" / "rates.sqlite",
        }
        self.urls = {
            "gateway": f"http://127.0.0.1:{self.ports['gateway']}",
            "ledger": f"http://127.0.0.1:{self.ports['ledger']}",
            "importer": f"http://127.0.0.1:{self.ports['importer']}",
            "enrichment": f"http://127.0.0.1:{self.ports['enrichment']}",
        }
        self.base_env = os.environ.copy()
        self.base_env.update({
            "PYTHONPATH": str(ROOT / "src"),
            "AI_PROVIDER": "local",
            "OPENAI_API_KEY": "",
            "GEMINI_API_KEY": "",
            "SPREADSHEET_ID": "",
            "GOOGLE_SHEETS_CREDENTIALS_B64": "",
            "GOOGLE_SHEETS_CREDENTIALS_FILE": str(root / "no-credentials.json"),
            "BASE_CURRENCY": "EUR",
            "DB_PATH": str(self.paths["db"]),
            "IMPORT_DB_PATH": str(self.paths["imports"]),
            "FX_DB_PATH": str(self.paths["fx"]),
            "LEDGER_URL": self.urls["ledger"],
            "IMPORT_SERVICE_URL": self.urls["importer"],
            "ENRICHMENT_URL": self.urls["enrichment"],
            "INTERNAL_API_TIMEOUT_SECONDS": "20",
            "MAX_UPLOAD_BYTES": str(1024 * 1024),
        })

    def start(self, name: str, module: str) -> None:
        self.start_process(name, module)
        self.wait_for(name, "/health/ready")

    def start_process(self, name: str, module: str) -> None:
        env = self.base_env.copy()
        env["SPECTRA_SERVICE_ROLE"] = name
        process = subprocess.Popen(
            [
                sys.executable, "-m", "uvicorn", module,
                "--host", "127.0.0.1", "--port", str(self.ports[name]),
                "--log-level", "error",
            ],
            cwd=ROOT,
            env=env,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        self.processes[name] = process

    def wait_for(self, name: str, endpoint: str) -> None:
        process = self.processes[name]
        url = f"{self.urls[name]}{endpoint}"
        deadline = time.monotonic() + 20
        while time.monotonic() < deadline:
            if process.poll() is not None:
                raise RuntimeError(f"{name} exited before readiness ({process.returncode})")
            try:
                with urlopen(url, timeout=0.5) as response:
                    if response.status == 200:
                        return
            except (OSError, URLError):
                time.sleep(0.05)
        raise RuntimeError(f"{name} did not become ready")

    def stop(self, name: str) -> None:
        process = self.processes.get(name)
        if process is not None and process.poll() is None:
            process.terminate()
            process.wait(timeout=5)

    def close(self) -> None:
        for name in reversed(tuple(self.processes)):
            process = self.processes[name]
            if process.poll() is None:
                process.terminate()
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=5)

    def settings(self) -> Settings:
        return Settings(
            service_role="gateway",
            ai_provider="local",
            db_path=self.paths["db"],
            import_db_path=self.paths["imports"],
            fx_db_path=self.paths["fx"],
            ledger_url=self.urls["ledger"],
            import_service_url=self.urls["importer"],
            enrichment_url=self.urls["enrichment"],
            google_sheets_credentials_file=str(self.root / "no-credentials.json"),
            internal_api_timeout_seconds=20,
            max_upload_bytes=1024 * 1024,
        )


@pytest.fixture
def service_stack(tmp_path: Path):
    stack = LocalServiceStack(tmp_path / "services")
    try:
        stack.start("ledger", "spectra.services.ledger.app:app")
        stack.start("enrichment", "spectra.services.enrichment.app:app")
        stack.start("importer", "spectra.services.importer.app:app")
        stack.start("gateway", "spectra.web.server:app")
        yield stack
    finally:
        stack.close()


def _events(content: str) -> list[dict]:
    import json

    return [
        json.loads(line[6:])
        for chunk in content.split("\n\n")
        for line in chunk.splitlines()
        if line.startswith("data: ")
    ]


def test_upload_review_commit_retry_and_cli_use_separate_services(service_stack: LocalServiceStack, tmp_path: Path) -> None:
    settings = service_stack.settings()
    gateway = service_stack.urls["gateway"]
    statement = (
        "Data Operazione;Descrizione;Importo;Valuta\n"
        "22/02/2026;POS STARBUCKS;-4,50;EUR\n"
    ).encode()

    with httpx.Client(base_url=gateway, timeout=30) as client:
        assert client.get("/health/ready").status_code == 200
        preference = client.patch("/api/settings/preferences", json={"base_currency": "EUR"})
        assert preference.status_code == 200

        upload = client.post(
            "/api/upload",
            files={"file": ("synthetic.csv", statement, "text/csv")},
        )
        assert upload.status_code == 200
        final = _events(upload.text)[-1]
        assert final["done"] is True
        assert len(final["transactions"]) == 1
        payload = {"job_id": final["job_id"], "transactions": final["transactions"]}

        invalid_payload = {
            "job_id": final["job_id"],
            "transactions": [dict(row) for row in final["transactions"]],
        }
        invalid_payload["transactions"][0]["amount"] = (
            float(invalid_payload["transactions"][0]["amount"]) + 1
        )
        rejected = client.post("/api/confirm", json=invalid_payload)
        assert rejected.status_code == 409
        assert "source field cannot be changed" in rejected.json()["message"]
        from spectra.services.importer.store import ImportJobStore
        assert ImportJobStore(service_stack.paths["imports"]).get(final["job_id"])["status"] == "ready"
        with BookmarkDB(service_stack.paths["db"]) as ledger:
            assert ledger.count() == 0

        first_commit = client.post("/api/confirm", json=payload)
        second_commit = client.post("/api/confirm", json=payload)
        assert first_commit.status_code == second_commit.status_code == 200
        assert first_commit.json()["ok"] is True
        assert second_commit.json()["ok"] is True
        transactions = client.get("/api/transactions?search=STARBUCKS").json()["transactions"]
        assert len(transactions) == 1
        assert transactions[0]["amount_decimal"] == "-4.50"
        assert transactions[0]["currency"] == "EUR"

        cli_statement = tmp_path / "cli-statement.csv"
        cli_statement.write_bytes(
            b"Data Operazione;Descrizione;Importo;Valuta\n"
            b"23/02/2026;POS BOOKSHOP;-8,00;EUR\n"
        )
        run_cli_import(settings, str(cli_statement), "EUR", False)
        run_cli_import(settings, str(cli_statement), "EUR", False)
        assert client.get("/api/transactions?search=BOOKSHOP").json()["total"] == 1

    with BookmarkDB(service_stack.paths["db"]) as ledger:
        assert ledger.count() == 2


def test_native_cli_launcher_starts_and_stops_the_four_process_stack(tmp_path: Path) -> None:
    stack = LocalServiceStack(tmp_path / "native-launcher")
    gateway_port = stack.ports["gateway"]
    environment = stack.base_env.copy()
    environment.update({
        "SPECTRA_LEDGER_PORT": str(stack.ports["ledger"]),
        "SPECTRA_IMPORTER_PORT": str(stack.ports["importer"]),
        "SPECTRA_ENRICHMENT_PORT": str(stack.ports["enrichment"]),
    })
    process = subprocess.Popen(
        [
            sys.executable, "-m", "spectra", "--serve", "--host", "127.0.0.1",
            "--port", str(gateway_port),
        ],
        cwd=ROOT,
        env=environment,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    try:
        deadline = time.monotonic() + 30
        ready_url = f"{stack.urls['gateway']}/health/ready"
        while time.monotonic() < deadline:
            if process.poll() is not None:
                raise RuntimeError("Native launcher exited before gateway readiness")
            try:
                with urlopen(ready_url, timeout=0.5) as response:
                    if response.status == 200:
                        break
            except (OSError, URLError):
                time.sleep(0.05)
        else:
            raise RuntimeError("Native launcher did not start the four services")
    finally:
        if process.poll() is None:
            process.send_signal(signal.SIGINT)
        try:
            process.wait(timeout=15)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait(timeout=5)

    assert process.returncode == 0


def test_service_readiness_and_gateway_errors_are_clear_when_ledger_stops(
    service_stack: LocalServiceStack,
) -> None:
    service_stack.stop("ledger")
    with httpx.Client(base_url=service_stack.urls["gateway"], timeout=5) as client:
        ready = client.get("/health/ready")
        assert ready.status_code == 503
        assert ready.json()["dependencies"]["ledger"] == "unavailable"
        summary = client.get("/api/summary?scope=90d")
        assert summary.status_code == 503
        assert summary.json()["service_unavailable"] is True


def test_ledger_backup_stream_cleans_its_temporary_snapshot(
    service_stack: LocalServiceStack, tmp_path: Path,
) -> None:
    temporary_root = Path(tempfile.gettempdir())
    before = set(temporary_root.glob("spectra-ledger-backup-*"))
    destination = tmp_path / "synthetic-ledger-backup.sqlite"
    with LedgerClient(service_stack.settings()) as ledger:
        ledger.download_backup(destination, max_bytes=10 * 1024 * 1024)

    assert destination.is_file()
    with BookmarkDB(destination) as backup:
        assert backup.count() == 0
    assert set(temporary_root.glob("spectra-ledger-backup-*")) == before


def test_gateway_and_cli_do_not_import_ledger_schema() -> None:
    gateway_source = (ROOT / "src/spectra/web/server.py").read_text(encoding="utf-8")
    cli_source = (ROOT / "src/spectra/pipeline.py").read_text(encoding="utf-8")
    importer_source = (ROOT / "src/spectra/import_service.py").read_text(encoding="utf-8")

    for source in (gateway_source, cli_source, importer_source):
        assert "spectra.db" not in source
        assert "._conn" not in source
        assert "SELECT " not in source


def test_non_ledger_process_startup_preserves_synthetic_legacy_store(tmp_path: Path) -> None:
    stack = LocalServiceStack(tmp_path / "non-ledger-startup")
    legacy_path = stack.paths["db"].with_name("prism.db")
    with BookmarkDB(legacy_path) as db:
        db.mark_seen("synthetic-legacy-row")

    try:
        for name, module, expected_status in (
            ("enrichment", "spectra.services.enrichment.app:app", 200),
            ("importer", "spectra.services.importer.app:app", 503),
            ("gateway", "spectra.web.server:app", 503),
        ):
            stack.start_process(name, module)
            stack.wait_for(name, "/health/live")
            with httpx.Client(base_url=stack.urls[name], timeout=5) as client:
                readiness = client.get("/health/ready")
            # Gateway/importer report their unavailable ledger dependency;
            # enrichment is independently ready. All three have loaded config.
            assert readiness.status_code == expected_status

        assert legacy_path.is_file()
        assert not stack.paths["db"].exists()
        with BookmarkDB(legacy_path) as db:
            assert db.is_seen("synthetic-legacy-row") is True
    finally:
        stack.close()
