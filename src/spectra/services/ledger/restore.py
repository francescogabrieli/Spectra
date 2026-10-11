"""Offline maintenance entrypoint for restoring the canonical ledger file."""

from __future__ import annotations

import argparse
from pathlib import Path

from spectra.backup import restore_backup
from spectra.config import load_settings


def restore(source: str | Path, destination: str | Path, *, max_bytes: int) -> Path:
    source_path = Path(source)
    destination_path = Path(destination)
    if not source_path.is_file():
        raise FileNotFoundError(f"Backup file does not exist: {source_path}")
    if source_path.stat().st_size > max_bytes:
        raise ValueError("Backup exceeds the configured size limit")
    for sidecar in (Path(f"{destination_path}-wal"), Path(f"{destination_path}-shm")):
        if sidecar.exists():
            raise FileExistsError(f"Stop Spectra and preserve the SQLite sidecar first: {sidecar}")
    return restore_backup(source_path, destination_path)


def main() -> None:
    parser = argparse.ArgumentParser(prog="spectra-ledger-restore")
    parser.add_argument("source", help="SQLite-aware Spectra backup file")
    parser.add_argument("--destination", required=True, help="Empty canonical ledger path")
    args = parser.parse_args()
    # Maintenance settings resolve the canonical path but never run the legacy
    # prism.db migration. The restore helper owns this one-shot file operation.
    settings = load_settings(service_role="maintenance")
    if Path(args.destination).resolve() != settings.db_path.resolve():
        parser.error("Restore destination must match the configured DB_PATH")
    try:
        restored = restore(args.source, settings.db_path, max_bytes=settings.max_backup_bytes)
    except (FileNotFoundError, FileExistsError, ValueError, OSError) as exc:
        parser.error(str(exc))
    print(f"Canonical ledger restored to {restored}")


if __name__ == "__main__":
    main()
