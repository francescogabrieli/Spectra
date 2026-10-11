"""Short-lived import staging store, separate from canonical financial data."""

from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any


class ImportJobStore:
    """Persists review drafts only; the ledger remains the financial source of truth."""

    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with sqlite3.connect(self.path, timeout=10) as connection:
            connection.execute(
                """CREATE TABLE IF NOT EXISTS import_jobs (
                    job_id TEXT PRIMARY KEY,
                    base_currency TEXT NOT NULL,
                    status TEXT NOT NULL,
                    transactions_json TEXT NOT NULL,
                    result_json TEXT NOT NULL DEFAULT '',
                    created_at TEXT NOT NULL,
                    expires_at TEXT NOT NULL
                )"""
            )
            connection.execute(
                "CREATE INDEX IF NOT EXISTS idx_import_jobs_expiry ON import_jobs(expires_at)"
            )

    def create(self, job_id: str, currency: str, transactions: list[dict[str, Any]]) -> None:
        now = datetime.now(timezone.utc)
        with sqlite3.connect(self.path, timeout=10) as connection:
            connection.execute("DELETE FROM import_jobs WHERE expires_at < ?", (now.isoformat(),))
            connection.execute(
                """INSERT OR REPLACE INTO import_jobs
                   (job_id, base_currency, status, transactions_json, created_at, expires_at)
                   VALUES (?, ?, 'ready', ?, ?, ?)""",
                (
                    job_id, currency.upper(), json.dumps(transactions, separators=(",", ":")),
                    now.isoformat(), (now + timedelta(days=7)).isoformat(),
                ),
            )

    def get(self, job_id: str) -> dict[str, Any] | None:
        now = datetime.now(timezone.utc).isoformat()
        with sqlite3.connect(self.path, timeout=10) as connection:
            row = connection.execute(
                """SELECT base_currency, status, transactions_json, result_json
                   FROM import_jobs WHERE job_id = ? AND expires_at >= ?""",
                (job_id, now),
            ).fetchone()
        if not row:
            return None
        return {
            "base_currency": str(row[0]),
            "status": str(row[1]),
            "transactions": json.loads(str(row[2])),
            "result": json.loads(str(row[3])) if row[3] else None,
        }

    def mark_committed(self, job_id: str, result: dict[str, Any]) -> None:
        with sqlite3.connect(self.path, timeout=10) as connection:
            connection.execute(
                "UPDATE import_jobs SET status = 'committed', result_json = ? WHERE job_id = ?",
                (json.dumps(result, separators=(",", ":")), job_id),
            )

