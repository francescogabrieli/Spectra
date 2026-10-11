"""CLI entry points for the shared local import service."""

from __future__ import annotations

import argparse
import asyncio
import logging
import os
import shutil
import socket
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlparse

from spectra.ai import CategorisedTransaction
from spectra.config import Settings, load_settings
from spectra.services.clients import ImportClient, LedgerClient, ServiceUnavailable
from spectra.reporter import generate_html_report

logger = logging.getLogger("spectra")


__all__ = [
    "main",
    "run",
    "run_inbox",
]


def _assert_services_stopped(settings: Settings) -> None:
    """Refuse offline restore while a known local service still owns the path."""
    ports = {8080, 8081, 8082, 8083}
    for name in ("SPECTRA_PORT", "SPECTRA_LEDGER_PORT", "SPECTRA_IMPORTER_PORT", "SPECTRA_ENRICHMENT_PORT"):
        value = os.environ.get(name)
        if value and value.isdigit():
            ports.add(int(value))
    for service_url in (settings.ledger_url, settings.import_service_url, settings.enrichment_url):
        parsed = urlparse(service_url)
        if parsed.hostname in {"127.0.0.1", "localhost", "::1"} and parsed.port:
            ports.add(parsed.port)
    for port in ports:
        try:
            with socket.create_connection(("127.0.0.1", port), timeout=0.1):
                raise RuntimeError(
                    "Stop all Spectra services before restoring the ledger backup."
                )
        except (ConnectionRefusedError, TimeoutError, OSError):
            continue


def run(settings: Settings, file: str, currency: str, dry_run: bool) -> None:
    """Send a bank export through the same local import API used by the web UI."""
    path = Path(file)
    logger.info("Reading %s export", path.suffix.upper())
    if not path.is_file():
        raise FileNotFoundError(f"Statement file does not exist: {path}")
    if path.stat().st_size > settings.max_upload_bytes:
        raise ValueError("Statement exceeds the configured upload size limit")

    async def send_import() -> dict:
        return await ImportClient(settings).cli_import(
            path.name, path.read_bytes(), currency or settings.base_currency, dry_run,
        )

    try:
        result = asyncio.run(send_import())
    except ServiceUnavailable as exc:
        raise RuntimeError(
            "Spectra services are not running. Start them with `spectra --serve` or Docker Compose."
        ) from exc

    if dry_run:
        transactions = [
            CategorisedTransaction.model_validate(row)
            for row in result.get("transactions", [])
        ]
        if transactions:
            logger.info("Dry run: generating a local preview report")
            generate_html_report(transactions, output_dir=settings.db_path.parent)
        logger.info("Dry run complete: %d transaction(s), no ledger changes", len(transactions))
    else:
        logger.info(
            "Local import complete: %s transaction(s) saved",
            result.get("transaction_count", 0),
        )


def run_inbox(settings: Settings, inbox_dir: str, currency: str, dry_run: bool) -> None:
    """Process supported files in an inbox, moving successful imports once."""
    inbox = Path(inbox_dir)
    if not inbox.exists():
        logger.info("Inbox folder does not exist: %s", inbox)
        return

    files = sorted(
        list(inbox.glob("*.csv")) + list(inbox.glob("*.pdf")) + list(inbox.glob("*.ofx"))
    )
    if not files:
        logger.info("No supported files in inbox")
        return

    processed = inbox.parent / "processed"
    processed.mkdir(exist_ok=True)
    logger.info("Found %d file(s) to process", len(files))
    for bank_file in files:
        try:
            run(settings, file=str(bank_file), currency=currency, dry_run=dry_run)
            if not dry_run:
                dest = processed / bank_file.name
                if dest.exists():
                    ts = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
                    dest = processed / f"{bank_file.stem}_{ts}{bank_file.suffix}"
                shutil.move(str(bank_file), str(dest))
        except Exception:
            logger.exception("Failed to process inbox file %s", bank_file.name)
    logger.info("Inbox processing complete")


def main() -> None:
    parser = argparse.ArgumentParser(
        prog="spectra",
        description="Spectra — local-first personal finance imports",
    )
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--file", "-f", help="Path to a bank CSV, PDF, or OFX export")
    group.add_argument("--inbox", help="Path to a folder with supported exports")
    group.add_argument("--serve", action="store_true", help="Launch the web dashboard")
    group.add_argument("--backup-db", metavar="PATH", help="Create a consistent SQLite backup")
    group.add_argument("--restore-db", metavar="PATH", help="Restore a backup if DB_PATH does not exist")
    parser.add_argument("--currency", default=None, help="Currency code (default: BASE_CURRENCY)")
    parser.add_argument("--dry-run", action="store_true", help="Generate a preview without saving")
    parser.add_argument("--port", type=int, default=8080, help="Dashboard port")
    parser.add_argument("--host", default="127.0.0.1", help="Dashboard bind address")
    args = parser.parse_args()
    if args.dry_run and (args.backup_db or args.restore_db):
        parser.error("--dry-run cannot be combined with a database backup operation")
    # The CLI is a gateway/adapter; only the ledger process may migrate or open
    # the canonical SQLite file.
    settings = load_settings(service_role="launcher" if args.serve else "gateway")

    if args.backup_db:
        with LedgerClient(settings) as ledger:
            ledger.download_backup(args.backup_db, max_bytes=settings.max_backup_bytes)
        logger.info("Canonical ledger backup created")
        return

    if args.restore_db:
        source = Path(args.restore_db)
        if not source.is_file():
            parser.error(f"Backup file does not exist: {source}")
        if source.stat().st_size > settings.max_backup_bytes:
            parser.error("Backup exceeds the configured size limit")
        try:
            _assert_services_stopped(settings)
            environment = os.environ.copy()
            environment["DB_PATH"] = str(settings.db_path)
            environment["SPECTRA_SERVICE_ROLE"] = "maintenance"
            subprocess.run(
                [
                    sys.executable, "-m", "spectra.services.ledger.restore",
                    str(source.resolve()), "--destination", str(settings.db_path),
                ],
                check=True,
                env=environment,
            )
        except (RuntimeError, subprocess.CalledProcessError) as exc:
            parser.error(str(exc))
        logger.info("Canonical ledger backup restored; restart the local services")
        return

    if args.serve:
        from spectra.launcher import launch_local_services

        launch_local_services(settings, host=args.host, port=args.port)
        return

    if args.currency:
        target_currency = args.currency.strip().upper()
    else:
        # The UI stores the user's selected denomination in the local DB.
        # The CLI follows the same preference unless explicitly overridden.
        with LedgerClient(settings) as ledger:
            target_currency = str(
                ledger.get_app_setting("base_currency") or settings.base_currency
            ).upper()
    try:
        if args.inbox:
            run_inbox(settings, inbox_dir=args.inbox, currency=target_currency, dry_run=args.dry_run)
        else:
            run(settings, file=args.file, currency=target_currency, dry_run=args.dry_run)
    except Exception:
        logger.exception("Import pipeline failed")
        sys.exit(1)


if __name__ == "__main__":
    main()
