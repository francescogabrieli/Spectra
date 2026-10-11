"""Dedicated SQLite persistence for cached exchange rates only."""

from __future__ import annotations

import sqlite3
from pathlib import Path
from typing import Any


class FXRateCache:
    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.connection = sqlite3.connect(self.path, timeout=10)
        self.connection.execute(
            """CREATE TABLE IF NOT EXISTS fx_rates (
                rate_date TEXT NOT NULL, base_currency TEXT NOT NULL,
                quote_currency TEXT NOT NULL, rate TEXT NOT NULL,
                effective_date TEXT NOT NULL, provider TEXT NOT NULL,
                PRIMARY KEY (rate_date, base_currency, quote_currency)
            )"""
        )
        self.connection.commit()

    def get_fx_rate(self, rate_date: str, base: str, quote: str) -> dict[str, Any] | None:
        row = self.connection.execute(
            """SELECT rate, effective_date, provider FROM fx_rates
               WHERE rate_date = ? AND base_currency = ? AND quote_currency = ?""",
            (rate_date, base, quote),
        ).fetchone()
        if not row:
            return None
        return {"rate": row[0], "effective_date": row[1], "provider": row[2]}

    def save_fx_rate(
        self, rate_date: str, base: str, quote: str, rate: str,
        effective_date: str, provider: str,
    ) -> None:
        with self.connection:
            self.connection.execute(
                """INSERT OR REPLACE INTO fx_rates
                   (rate_date, base_currency, quote_currency, rate, effective_date, provider)
                   VALUES (?, ?, ?, ?, ?, ?)""",
                (rate_date, base, quote, rate, effective_date, provider),
            )

    def close(self) -> None:
        self.connection.close()

    def __enter__(self):
        return self

    def __exit__(self, *_exc):
        self.close()
