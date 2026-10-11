"""Consistent SQLite backup and safe, non-overwriting restore helpers."""

from __future__ import annotations

import os
import sqlite3
import tempfile
from contextlib import closing
from pathlib import Path


def _readonly_connection(path: Path) -> sqlite3.Connection:
    if not path.is_file():
        raise FileNotFoundError(f"Database file does not exist: {path}")
    return sqlite3.connect(f"{path.resolve().as_uri()}?mode=ro", uri=True)


def _validate_database(connection: sqlite3.Connection) -> None:
    check = connection.execute("PRAGMA quick_check").fetchone()
    if not check or check[0] != "ok":
        raise ValueError("SQLite database failed its integrity check")
    tables = {
        str(row[0])
        for row in connection.execute("SELECT name FROM sqlite_master WHERE type = 'table'")
    }
    if "tx_history" not in tables or "seen_transactions" not in tables:
        raise ValueError("File is not a Spectra transaction database")


def _copy_database(source_path: Path, target_path: Path) -> None:
    with closing(_readonly_connection(source_path)) as source:
        _validate_database(source)
        with closing(sqlite3.connect(target_path)) as target:
            source.backup(target)
            target.commit()
            _validate_database(target)


def _publish_new_database(source_path: Path, target_path: Path) -> Path:
    if source_path.resolve() == target_path.resolve():
        raise ValueError("Source and destination must be different files")
    target_path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary_name = tempfile.mkstemp(
        prefix=f".{target_path.name}.", suffix=".tmp", dir=target_path.parent,
    )
    os.close(fd)
    temporary_path = Path(temporary_name)
    os.chmod(temporary_path, 0o600)
    try:
        _copy_database(source_path, temporary_path)
        # A hard link publishes the complete file atomically and fails rather
        # than replacing an existing backup or destination.
        os.link(temporary_path, target_path)
        temporary_path.unlink()
    except BaseException:
        temporary_path.unlink(missing_ok=True)
        raise
    return target_path


def create_backup(source: str | Path, destination: str | Path) -> Path:
    """Create a consistent SQLite snapshot without overwriting an existing file."""
    return _publish_new_database(Path(source), Path(destination))


def restore_backup(source: str | Path, destination: str | Path) -> Path:
    """Restore a validated snapshot, refusing to overwrite the target database."""
    return _publish_new_database(Path(source), Path(destination))
