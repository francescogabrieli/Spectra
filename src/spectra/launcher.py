"""Start the four local Spectra services as independent Python processes."""

from __future__ import annotations

import os
import subprocess
import sys
import time
from collections.abc import Sequence
from urllib.error import URLError
from urllib.request import urlopen

from spectra.config import Settings


_SERVICES = (
    ("ledger", "spectra.services.ledger.app:app"),
    ("enrichment", "spectra.services.enrichment.app:app"),
    ("importer", "spectra.services.importer.app:app"),
)


def _port(env_name: str, default: int) -> int:
    value = int(os.environ.get(env_name, default))
    if not 1 <= value <= 65535:
        raise ValueError(f"{env_name} must be between 1 and 65535")
    return value


def _child_environment(settings: Settings, service: str, ports: dict[str, int]) -> dict[str, str]:
    environment = os.environ.copy()
    # The launcher is the only parent process that needs to read both optional
    # integrations' configuration. Pass each secret only to its owner instead
    # of inheriting the user's complete environment into every service.
    for name in (
        "OPENAI_API_KEY",
        "GEMINI_API_KEY",
        "SPREADSHEET_ID",
        "GOOGLE_SHEETS_CREDENTIALS_B64",
        "GOOGLE_SHEETS_CREDENTIALS_FILE",
    ):
        environment.pop(name, None)
    environment.update({
        "SPECTRA_SERVICE_ROLE": service,
        "DB_PATH": str(settings.db_path),
        "IMPORT_DB_PATH": str(settings.import_db_path),
        "FX_DB_PATH": str(settings.fx_db_path),
        "LEDGER_URL": f"http://127.0.0.1:{ports['ledger']}",
        "IMPORT_SERVICE_URL": f"http://127.0.0.1:{ports['importer']}",
        "ENRICHMENT_URL": f"http://127.0.0.1:{ports['enrichment']}",
    })
    if service == "enrichment":
        environment.update({
            "AI_PROVIDER": settings.ai_provider,
            "OPENAI_API_KEY": settings.openai_api_key,
            "OPENAI_MODEL": settings.openai_model,
            "GEMINI_API_KEY": settings.gemini_api_key,
            "GEMINI_MODEL": settings.gemini_model,
        })
    elif service == "importer":
        environment.update({
            "SPREADSHEET_ID": settings.spreadsheet_id,
            "GOOGLE_SHEETS_CREDENTIALS_B64": settings.google_sheets_credentials_b64,
            "GOOGLE_SHEETS_CREDENTIALS_FILE": settings.google_sheets_credentials_file,
        })
    return environment


def _ready(url: str, process: subprocess.Popen, timeout: float = 30.0) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if process.poll() is not None:
            raise RuntimeError(f"Spectra service exited before readiness: {url}")
        try:
            with urlopen(url, timeout=0.5) as response:
                if response.status == 200:
                    return
        except (OSError, URLError):
            time.sleep(0.1)
    raise RuntimeError(f"Timed out waiting for local service readiness: {url}")


def launch_local_services(settings: Settings, *, host: str = "127.0.0.1", port: int = 8080) -> None:
    """Run ledger, enrichment, importer, and gateway until interrupted."""
    ports = {
        "ledger": _port("SPECTRA_LEDGER_PORT", 8081),
        "importer": _port("SPECTRA_IMPORTER_PORT", 8082),
        "enrichment": _port("SPECTRA_ENRICHMENT_PORT", 8083),
    }
    if port in ports.values():
        raise ValueError("Gateway port must differ from internal service ports")

    children: list[subprocess.Popen] = []
    try:
        for service, app_path in _SERVICES:
            service_port = ports[service]
            command = [
                sys.executable, "-m", "uvicorn", app_path,
                "--host", "127.0.0.1", "--port", str(service_port),
                "--log-level", "warning",
            ]
            environment = _child_environment(settings, service, ports)
            children.append(subprocess.Popen(command, env=environment))
            _ready(f"http://127.0.0.1:{service_port}/health/ready", children[-1])

        gateway_environment = _child_environment(settings, "gateway", ports)
        command = [
            sys.executable, "-m", "uvicorn", "spectra.web.server:app",
            "--host", host, "--port", str(port), "--log-level", "warning",
        ]
        children.append(subprocess.Popen(command, env=gateway_environment))
        _ready(f"http://127.0.0.1:{port}/health/ready", children[-1])
        print(f"\n  Spectra is ready at http://{host}:{port}\n", flush=True)
        while True:
            for child in children:
                if child.poll() is not None:
                    raise RuntimeError("A Spectra service stopped unexpectedly")
            time.sleep(0.5)
    except KeyboardInterrupt:
        pass
    finally:
        for child in reversed(children):
            if child.poll() is None:
                child.terminate()
        for child in reversed(children):
            try:
                child.wait(timeout=5)
            except subprocess.TimeoutExpired:
                child.kill()
                child.wait(timeout=5)


def main(argv: Sequence[str] | None = None) -> None:
    """Entrypoint reserved for direct launcher execution."""
    del argv
    from spectra.config import load_settings

    # Launcher config may read the optional secrets so it can distribute them
    # to their owning child only. Its Settings role deliberately cannot migrate
    # the canonical financial store.
    launch_local_services(load_settings(service_role="launcher"))


if __name__ == "__main__":
    main()
