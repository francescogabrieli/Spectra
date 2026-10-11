"""SQLite bookmark — tracks which transactions have already been imported."""

from __future__ import annotations

import logging
import sqlite3
import hashlib
import json
from pathlib import Path
from typing import Any

from spectra.money import as_decimal, decimal_text, minor_units

logger = logging.getLogger("spectra.db")

_SCHEMA = """
CREATE TABLE IF NOT EXISTS seen_transactions (
    tx_id       TEXT PRIMARY KEY,
    source      TEXT NOT NULL,          -- e.g. "CSV"
    seen_at     TEXT NOT NULL           -- ISO-8601 UTC timestamp
);

CREATE TABLE IF NOT EXISTS tx_history (
    tx_id       TEXT PRIMARY KEY,
    date        TEXT NOT NULL,
    clean_name  TEXT NOT NULL,
    amount      REAL NOT NULL,
    amount_currency TEXT,
    amount_decimal TEXT,
    amount_minor INTEGER,
    category    TEXT NOT NULL DEFAULT 'Uncategorized',
    original_description TEXT NOT NULL DEFAULT '',
    counterpart TEXT NOT NULL DEFAULT '',
    recurring TEXT NOT NULL DEFAULT '',
    classification_source TEXT NOT NULL DEFAULT '',
    category_confidence REAL,
    needs_review INTEGER NOT NULL DEFAULT 0,
    review_reason TEXT NOT NULL DEFAULT '',
    account_name TEXT NOT NULL DEFAULT '',
    import_batch_id TEXT NOT NULL DEFAULT '',
    transfer_group_id TEXT NOT NULL DEFAULT '',
    transfer_status TEXT NOT NULL DEFAULT 'none',
    excluded_from_spend INTEGER NOT NULL DEFAULT 0,
    original_amount REAL,
    original_currency TEXT,
    original_amount_decimal TEXT,
    original_amount_minor INTEGER,
    conversion_rate REAL,
    conversion_date TEXT,
    conversion_source TEXT NOT NULL DEFAULT '',
    conversion_error TEXT NOT NULL DEFAULT '',
    currency_source TEXT NOT NULL DEFAULT '',
    account_id TEXT NOT NULL DEFAULT ''
);

CREATE TABLE IF NOT EXISTS accounts (
    account_id TEXT PRIMARY KEY,
    display_name TEXT NOT NULL DEFAULT '',
    currency TEXT NOT NULL,
    balance REAL,
    balance_decimal TEXT,
    balance_as_of TEXT,
    balance_source TEXT NOT NULL DEFAULT ''
);

CREATE TABLE IF NOT EXISTS fx_rates (
    rate_date TEXT NOT NULL,
    base_currency TEXT NOT NULL,
    quote_currency TEXT NOT NULL,
    rate TEXT NOT NULL,
    effective_date TEXT NOT NULL,
    provider TEXT NOT NULL,
    PRIMARY KEY (rate_date, base_currency, quote_currency)
);

CREATE TABLE IF NOT EXISTS user_overrides (
    original_description TEXT PRIMARY KEY,
    category             TEXT NOT NULL,
    clean_name           TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS merchant_categories (
    clean_name  TEXT PRIMARY KEY,
    category    TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS budget_limits (
    category    TEXT PRIMARY KEY,
    monthly_limit REAL NOT NULL,
    limit_currency TEXT,
    limit_decimal TEXT
);

CREATE TABLE IF NOT EXISTS app_settings (
    key         TEXT PRIMARY KEY,
    value       TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS category_rules (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    rule_type   TEXT NOT NULL,     -- contains | regex
    pattern     TEXT NOT NULL,
    category    TEXT NOT NULL,
    priority    INTEGER NOT NULL DEFAULT 100,
    is_active   INTEGER NOT NULL DEFAULT 1,
    created_at  TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS learning_feedback (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    tx_id               TEXT,
    original_description TEXT NOT NULL DEFAULT '',
    clean_name          TEXT NOT NULL,
    category            TEXT NOT NULL,
    source              TEXT NOT NULL,
    apply_to_future     INTEGER NOT NULL DEFAULT 1,
    created_at          TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS import_commits (
    idempotency_key TEXT PRIMARY KEY,
    result_json TEXT NOT NULL,
    committed_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);
"""

_TX_HISTORY_COLUMN_DEFS: dict[str, str] = {
    "category": "TEXT NOT NULL DEFAULT 'Uncategorized'",
    "original_description": "TEXT NOT NULL DEFAULT ''",
    "counterpart": "TEXT NOT NULL DEFAULT ''",
    "recurring": "TEXT NOT NULL DEFAULT ''",
    "classification_source": "TEXT NOT NULL DEFAULT ''",
    "category_confidence": "REAL",
    "needs_review": "INTEGER NOT NULL DEFAULT 0",
    "review_reason": "TEXT NOT NULL DEFAULT ''",
    "account_name": "TEXT NOT NULL DEFAULT ''",
    "import_batch_id": "TEXT NOT NULL DEFAULT ''",
    "transfer_group_id": "TEXT NOT NULL DEFAULT ''",
    "transfer_status": "TEXT NOT NULL DEFAULT 'none'",
    "excluded_from_spend": "INTEGER NOT NULL DEFAULT 0",
    "original_amount": "REAL",
    "original_currency": "TEXT",
    "amount_currency": "TEXT",
    "amount_decimal": "TEXT",
    "amount_minor": "INTEGER",
    "original_amount_decimal": "TEXT",
    "original_amount_minor": "INTEGER",
    "conversion_rate": "REAL",
    "conversion_date": "TEXT",
    "conversion_source": "TEXT NOT NULL DEFAULT ''",
    "conversion_error": "TEXT NOT NULL DEFAULT ''",
    "currency_source": "TEXT NOT NULL DEFAULT ''",
    "account_id": "TEXT NOT NULL DEFAULT ''",
}

_TX_HISTORY_SELECT_COLUMNS = """
tx_id,
date,
clean_name,
amount,
category,
original_description,
counterpart,
recurring,
classification_source,
category_confidence,
needs_review,
review_reason,
account_name,
import_batch_id,
transfer_group_id,
transfer_status,
excluded_from_spend,
original_amount,
original_currency,
amount_currency,
amount_decimal,
amount_minor,
original_amount_decimal,
original_amount_minor,
conversion_rate,
conversion_date,
conversion_source,
conversion_error,
currency_source,
account_id
"""


class BookmarkDB:
    """Thin wrapper around a SQLite database for dedup tracking."""

    def __init__(self, db_path: Path | str) -> None:
        self._path = Path(db_path)
        self._path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(str(self._path))
        self._conn.executescript(_SCHEMA)
        self._conn.commit()
        self._migrate()
        logger.info("Bookmark DB ready at %s", self._path)

    def _migrate(self) -> None:
        """Apply backwards-compatible schema migrations."""
        existing_columns = {
            str(row[1])
            for row in self._conn.execute("PRAGMA table_info(tx_history)").fetchall()
        }
        for column, definition in _TX_HISTORY_COLUMN_DEFS.items():
            if column in existing_columns:
                continue
            self._conn.execute(f"ALTER TABLE tx_history ADD COLUMN {column} {definition}")
        # Currency of the stored amount is authoritative. Historical rows are
        # left unknown unless they already carry an explicit currency field;
        # the current preference is not proof of the currency used at import.
        self._conn.execute(
            "UPDATE tx_history SET currency_source = 'legacy-unknown' "
            "WHERE amount_currency IS NULL AND currency_source = ''"
        )
        self._conn.execute(
            """UPDATE tx_history SET needs_review = 1,
               review_reason = 'legacy_currency_unknown'
               WHERE amount_currency IS NULL AND currency_source = 'legacy-unknown'"""
        )
        # budget_limits table (for existing DBs pre-budget feature)
        self._conn.execute("""
            CREATE TABLE IF NOT EXISTS budget_limits (
                category      TEXT PRIMARY KEY,
                monthly_limit REAL NOT NULL,
                limit_currency TEXT,
                limit_decimal TEXT
            )
        """)
        budget_columns = {
            str(row[1])
            for row in self._conn.execute("PRAGMA table_info(budget_limits)").fetchall()
        }
        for column in ("limit_currency", "limit_decimal"):
            if column not in budget_columns:
                self._conn.execute(f"ALTER TABLE budget_limits ADD COLUMN {column} TEXT")
        self._conn.execute("""
            CREATE TABLE IF NOT EXISTS app_settings (
                key   TEXT PRIMARY KEY,
                value TEXT NOT NULL
            )
        """)
        self._conn.execute("""
            CREATE TABLE IF NOT EXISTS category_rules (
                id         INTEGER PRIMARY KEY AUTOINCREMENT,
                rule_type  TEXT NOT NULL,
                pattern    TEXT NOT NULL,
                category   TEXT NOT NULL,
                priority   INTEGER NOT NULL DEFAULT 100,
                is_active  INTEGER NOT NULL DEFAULT 1,
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
            )
        """)
        self._conn.execute("""
            CREATE TABLE IF NOT EXISTS learning_feedback (
                id                   INTEGER PRIMARY KEY AUTOINCREMENT,
                tx_id                TEXT,
                original_description TEXT NOT NULL DEFAULT '',
                clean_name           TEXT NOT NULL,
                category             TEXT NOT NULL,
                source               TEXT NOT NULL,
                apply_to_future      INTEGER NOT NULL DEFAULT 1,
                created_at           TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
            )
        """)
        self._conn.executescript(
            """
            CREATE INDEX IF NOT EXISTS idx_tx_history_date_desc ON tx_history(date DESC);
            CREATE INDEX IF NOT EXISTS idx_tx_history_category ON tx_history(category);
            CREATE INDEX IF NOT EXISTS idx_tx_history_needs_review ON tx_history(needs_review, date DESC);
            CREATE INDEX IF NOT EXISTS idx_tx_history_account_name ON tx_history(account_name);
            CREATE INDEX IF NOT EXISTS idx_tx_history_transfer_status ON tx_history(transfer_status);
            CREATE INDEX IF NOT EXISTS idx_tx_history_import_batch ON tx_history(import_batch_id);
            CREATE INDEX IF NOT EXISTS idx_tx_history_currency_date ON tx_history(amount_currency, date);
            CREATE INDEX IF NOT EXISTS idx_tx_history_account_id ON tx_history(account_id);
            """
        )
        self._conn.commit()

    @staticmethod
    def _row_to_transaction(row: tuple[Any, ...]) -> dict[str, Any]:
        return {
            "id": str(row[0]),
            "date": str(row[1]),
            "merchant": str(row[2]),
            "amount": float(row[3]),
            "category": str(row[4]),
            "original_description": str(row[5] or ""),
            "counterpart": str(row[6] or ""),
            "recurring": str(row[7] or ""),
            "classification_source": str(row[8] or ""),
            "category_confidence": float(row[9]) if row[9] is not None else None,
            "needs_review": bool(row[10]),
            "review_reason": str(row[11] or ""),
            "account_name": str(row[12] or ""),
            "import_batch_id": str(row[13] or ""),
            "transfer_group_id": str(row[14] or ""),
            "transfer_status": str(row[15] or "none"),
            "excluded_from_spend": bool(row[16]),
            "original_amount": float(row[17]) if row[17] is not None else None,
            "original_currency": str(row[18] or "") or None,
            "currency": str(row[19] or "") or None,
            "amount_decimal": str(row[20] or "") or None,
            "amount_minor": int(row[21]) if row[21] is not None else None,
            "original_amount_decimal": str(row[22] or "") or None,
            "original_amount_minor": int(row[23]) if row[23] is not None else None,
            "conversion_rate": float(row[24]) if row[24] is not None else None,
            "conversion_date": str(row[25] or "") or None,
            "conversion_source": str(row[26] or ""),
            "conversion_error": str(row[27] or ""),
            "currency_source": str(row[28] or ""),
            "account_id": str(row[29] or ""),
        }

    # ── Transaction dedup ────────────────────────────────────────

    def is_seen(self, tx_id: str) -> bool:
        """Return True if this transaction ID was already processed."""
        row = self._conn.execute(
            "SELECT 1 FROM seen_transactions WHERE tx_id = ?", (tx_id,)
        ).fetchone()
        return row is not None

    def mark_seen(self, tx_id: str, source: str = "CSV") -> None:
        """Record that *tx_id* has been processed."""
        from datetime import datetime, timezone

        self._conn.execute(
            """
            INSERT OR IGNORE INTO seen_transactions (tx_id, source, seen_at)
            VALUES (?, ?, ?)
            """,
            (tx_id, source, datetime.now(timezone.utc).isoformat()),
        )
        self._conn.commit()

    def mark_seen_batch(self, tx_ids: list[str], source: str = "CSV") -> None:
        """Record a batch of transaction IDs as processed."""
        from datetime import datetime, timezone

        now = datetime.now(timezone.utc).isoformat()
        self._conn.executemany(
            """
            INSERT OR IGNORE INTO seen_transactions (tx_id, source, seen_at)
            VALUES (?, ?, ?)
            """,
            [(tx_id, source, now) for tx_id in tx_ids],
        )
        self._conn.commit()

    # ── History tracking for Recurring Detection ─────────────────

    def _write_history_rows(self, transactions: list[Any]) -> None:
        from datetime import datetime, timezone

        columns = """
            tx_id, date, clean_name, amount, amount_currency, amount_decimal, amount_minor,
            category, original_description, counterpart, recurring, classification_source,
            category_confidence, needs_review, review_reason, account_name, import_batch_id,
            transfer_group_id, transfer_status, excluded_from_spend, original_amount,
            original_currency, original_amount_decimal, original_amount_minor,
            conversion_rate, conversion_date, conversion_source, conversion_error,
            currency_source, account_id
        """
        placeholders = ", ".join("?" for _ in range(30))
        rows = []
        account_rows: dict[str, tuple[Any, ...]] = {}
        for tx in transactions:
            amount_currency = str(
                getattr(tx, "currency", None)
                or getattr(tx, "amount_currency", None)
                or self.get_app_setting("base_currency")
                or "EUR"
            ).upper()
            amount_text = decimal_text(
                getattr(tx, "amount_decimal", None) or getattr(tx, "amount"),
                amount_currency,
            )
            raw_original_amount = getattr(tx, "original_amount", None)
            original_currency = getattr(tx, "original_currency", None)
            original_text = (
                decimal_text(
                    getattr(tx, "original_amount_decimal", None) or raw_original_amount,
                    original_currency,
                )
                if raw_original_amount is not None and original_currency
                else None
            )
            account_id = str(getattr(tx, "account_id", "") or "")
            account_name = str(getattr(tx, "account_name", "") or "")
            account_balance = getattr(tx, "account_balance", None)
            account_balance_date = str(getattr(tx, "account_balance_date", "") or "")
            if account_name and not account_id:
                account_id = "acct-" + hashlib.sha256(
                    f"{account_name.casefold()}|{amount_currency}".encode("utf-8")
                ).hexdigest()[:20]
            if account_id:
                balance_text = (
                    decimal_text(account_balance, amount_currency)
                    if account_balance is not None
                    else None
                )
                account_rows[account_id] = (
                    account_id, account_name, amount_currency,
                    float(account_balance) if account_balance is not None else None,
                    balance_text, account_balance_date or None,
                    "statement" if account_balance is not None else "",
                )
            rows.append((
                str(tx.id), str(tx.date), str(tx.clean_name), float(tx.amount),
                amount_currency, amount_text, minor_units(amount_text, amount_currency),
                str(getattr(tx, "category", "Uncategorized") or "Uncategorized"),
                str(getattr(tx, "original_description", "") or ""),
                str(getattr(tx, "counterpart", "") or ""),
                str(getattr(tx, "recurring", "") or ""),
                str(getattr(tx, "classification_source", "") or ""),
                getattr(tx, "category_confidence", None),
                1 if getattr(tx, "needs_review", False) else 0,
                str(getattr(tx, "review_reason", "") or ""),
                account_name, str(getattr(tx, "import_batch_id", "") or ""),
                str(getattr(tx, "transfer_group_id", "") or ""),
                str(getattr(tx, "transfer_status", "none") or "none"),
                1 if getattr(tx, "excluded_from_spend", False) else 0,
                float(raw_original_amount) if raw_original_amount is not None else None,
                str(original_currency or "") or None, original_text,
                minor_units(original_text, original_currency) if original_text else None,
                getattr(tx, "conversion_rate", None), getattr(tx, "conversion_date", None),
                str(getattr(tx, "conversion_source", "") or ""),
                str(getattr(tx, "conversion_error", "") or ""),
                str(getattr(tx, "currency_source", "parser") or "parser"), account_id,
            ))

        if rows:
            self._conn.executemany(
                f"INSERT OR REPLACE INTO tx_history ({columns}) VALUES ({placeholders})", rows,
            )
            now = datetime.now(timezone.utc).isoformat()
            self._conn.executemany(
                "INSERT OR IGNORE INTO seen_transactions (tx_id, source, seen_at) VALUES (?, ?, ?)",
                [(row[0], "IMPORT", now) for row in rows],
            )
        for row in account_rows.values():
            self._conn.execute(
                """INSERT INTO accounts (
                    account_id, display_name, currency, balance, balance_decimal,
                    balance_as_of, balance_source
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(account_id) DO UPDATE SET
                    display_name = CASE WHEN excluded.display_name != '' THEN excluded.display_name ELSE accounts.display_name END,
                    currency = excluded.currency,
                    balance = CASE WHEN excluded.balance IS NOT NULL THEN excluded.balance ELSE accounts.balance END,
                    balance_decimal = CASE WHEN excluded.balance_decimal IS NOT NULL THEN excluded.balance_decimal ELSE accounts.balance_decimal END,
                    balance_as_of = CASE WHEN excluded.balance_as_of IS NOT NULL THEN excluded.balance_as_of ELSE accounts.balance_as_of END,
                    balance_source = CASE WHEN excluded.balance_source != '' THEN excluded.balance_source ELSE accounts.balance_source END
                """,
                row,
            )

    def save_history(self, transactions: list[Any]) -> None:
        """Save rows, account metadata, and idempotency markers atomically."""
        with self._conn:
            self._write_history_rows(transactions)

    def save_import_atomic(
        self,
        transactions: list[Any],
        *,
        learning_feedback: list[dict[str, Any]] | None = None,
        merchant_mappings: dict[str, str] | None = None,
        overrides: dict[str, dict[str, str]] | None = None,
        existing_transfer_updates: list[dict[str, Any]] | None = None,
    ) -> None:
        """Commit an accepted import and its learning side effects in one transaction."""
        with self._conn:
            self._write_import_side_effects(
                transactions,
                learning_feedback=learning_feedback,
                merchant_mappings=merchant_mappings,
                overrides=overrides,
                existing_transfer_updates=existing_transfer_updates,
            )

    def _write_import_side_effects(
        self,
        transactions: list[Any],
        *,
        learning_feedback: list[dict[str, Any]] | None,
        merchant_mappings: dict[str, str] | None,
        overrides: dict[str, dict[str, str]] | None,
        existing_transfer_updates: list[dict[str, Any]] | None,
    ) -> None:
        self._write_history_rows(transactions)
        for update in existing_transfer_updates or []:
            fields = {key: value for key, value in update.items() if key != "tx_id"}
            if fields:
                assignments = ", ".join(f"{key} = ?" for key in fields)
                self._conn.execute(
                    f"UPDATE tx_history SET {assignments} WHERE tx_id = ?",
                    [*fields.values(), str(update["tx_id"])],
                )
        if merchant_mappings:
            self._conn.executemany(
                "INSERT OR REPLACE INTO merchant_categories (clean_name, category) VALUES (?, ?)",
                list(merchant_mappings.items()),
            )
        if overrides:
            self._conn.executemany(
                """INSERT OR REPLACE INTO user_overrides
                   (original_description, category, clean_name) VALUES (?, ?, ?)""",
                [(desc, value.get("category", ""), value.get("clean_name", ""))
                 for desc, value in overrides.items()],
            )
        for feedback in learning_feedback or []:
            self._conn.execute(
                """INSERT INTO learning_feedback (
                    tx_id, original_description, clean_name, category, source, apply_to_future
                ) VALUES (?, ?, ?, ?, ?, ?)""",
                (
                    feedback.get("tx_id"), feedback.get("original_description", ""),
                    feedback.get("clean_name", ""), feedback.get("category", ""),
                    feedback.get("source", "upload_confirm"),
                    1 if feedback.get("apply_to_future", True) else 0,
                ),
            )

    def commit_import_once(
        self,
        idempotency_key: str,
        transactions: list[Any],
        *,
        learning_feedback: list[dict[str, Any]] | None = None,
        merchant_mappings: dict[str, str] | None = None,
        overrides: dict[str, dict[str, str]] | None = None,
        existing_transfer_updates: list[dict[str, Any]] | None = None,
    ) -> dict[str, Any]:
        """Atomically commit financial rows and an idempotent retry receipt."""
        key = str(idempotency_key).strip()
        if not key or len(key) > 200:
            raise ValueError("A non-empty import idempotency key is required")
        with self._conn:
            existing = self._conn.execute(
                "SELECT result_json FROM import_commits WHERE idempotency_key = ?", (key,),
            ).fetchone()
            if existing:
                result = json.loads(str(existing[0]))
                return {**result, "already_committed": True}
            self._write_import_side_effects(
                transactions,
                learning_feedback=learning_feedback,
                merchant_mappings=merchant_mappings,
                overrides=overrides,
                existing_transfer_updates=existing_transfer_updates,
            )
            result = {"ok": True, "transaction_count": len(transactions)}
            self._conn.execute(
                "INSERT INTO import_commits (idempotency_key, result_json) VALUES (?, ?)",
                (key, json.dumps(result, separators=(",", ":"))),
            )
        return {**result, "already_committed": False}

    def list_known_categories(self) -> list[str]:
        rows = self._conn.execute(
            "SELECT DISTINCT category FROM tx_history WHERE category != 'Uncategorized' ORDER BY category"
        ).fetchall()
        return [str(row[0]) for row in rows]

    def read_model(self, name: str, **params: Any) -> Any:
        """Return allowlisted read models used by the gateway; no SQL crosses HTTP."""
        if name == "transaction_count":
            return int(self._conn.execute("SELECT COUNT(*) FROM tx_history").fetchone()[0])
        if name == "categories":
            return self.list_known_categories()
        if name == "setup_summary":
            return {
                "transaction_count": int(self._conn.execute("SELECT COUNT(*) FROM tx_history").fetchone()[0]),
                "legacy_currency_count": int(self._conn.execute(
                    "SELECT COUNT(*) FROM tx_history WHERE amount_currency IS NULL AND currency_source = 'legacy-unknown'"
                ).fetchone()[0]),
                "merchant_count": int(self._conn.execute("SELECT COUNT(*) FROM merchant_categories").fetchone()[0]),
                "feedback_count": int(self._conn.execute("SELECT COUNT(*) FROM learning_feedback").fetchone()[0]),
                "active_rule_count": int(self._conn.execute("SELECT COUNT(*) FROM category_rules WHERE is_active = 1").fetchone()[0]),
                "categories": self.list_known_categories(),
            }
        if name == "learning_summary":
            return {
                "feedback_count": int(self._conn.execute("SELECT COUNT(*) FROM learning_feedback").fetchone()[0]),
                "override_count": int(self._conn.execute("SELECT COUNT(*) FROM user_overrides").fetchone()[0]),
                "uncategorized_count": int(self._conn.execute("SELECT COUNT(*) FROM tx_history WHERE category = 'Uncategorized'").fetchone()[0]),
                "learned_future_count": int(self._conn.execute("SELECT COUNT(*) FROM learning_feedback WHERE apply_to_future = 1").fetchone()[0]),
            }
        if name == "transaction_merchants":
            ids = [str(value) for value in params.get("ids", [])]
            if not ids:
                return []
            placeholders = ",".join("?" for _ in ids)
            rows = self._conn.execute(
                f"SELECT tx_id, clean_name, original_description FROM tx_history WHERE tx_id IN ({placeholders})",
                ids,
            ).fetchall()
            return [list(row) for row in rows]
        if name == "rule_impact_rows":
            return [list(row) for row in self._conn.execute(
                "SELECT tx_id, date, clean_name, original_description, category FROM tx_history ORDER BY date DESC, tx_id DESC"
            ).fetchall()]
        currency = str(params.get("currency", "")).upper()
        if name == "summary_rows":
            return [list(row) for row in self._conn.execute(
                """SELECT date, clean_name, COALESCE(amount_decimal, CAST(amount AS TEXT)), category
                   FROM tx_history WHERE excluded_from_spend = 0 AND amount_currency = ? ORDER BY date DESC""",
                (currency,),
            ).fetchall()]
        if name == "summary_non_base_count":
            return int(self._conn.execute(
                """SELECT COUNT(*) FROM tx_history WHERE excluded_from_spend = 0 AND date >= ? AND date < ?
                   AND (amount_currency IS NULL OR amount_currency != ?)""",
                (str(params["start"]), str(params["end"]), currency),
            ).fetchone()[0])
        if name == "budget_expense_rows":
            return [list(row) for row in self._conn.execute(
                """SELECT category, amount_decimal, amount FROM tx_history
                   WHERE amount < 0 AND excluded_from_spend = 0 AND date >= ? AND date < ? AND amount_currency = ?""",
                (str(params["start"]), str(params["end"]), currency),
            ).fetchall()]
        if name == "budget_categories":
            return [str(row[0]) for row in self._conn.execute(
                """SELECT DISTINCT category FROM tx_history
                   WHERE category != 'Uncategorized' AND amount < 0 AND excluded_from_spend = 0 AND amount_currency = ?
                   ORDER BY category""", (currency,),
            ).fetchall()]
        if name == "currency_count":
            return int(self._conn.execute(
                """SELECT COUNT(*) FROM tx_history WHERE excluded_from_spend = 0 AND date >= ? AND date < ?
                   AND (amount_currency IS NULL OR amount_currency != ?)""",
                (str(params["start"]), str(params["end"]), currency),
            ).fetchone()[0])
        if name == "all_time_currency_count":
            return int(self._conn.execute(
                """SELECT COUNT(*) FROM tx_history WHERE excluded_from_spend = 0
                   AND (amount_currency IS NULL OR amount_currency != ?)""", (currency,),
            ).fetchone()[0])
        if name == "trend_rows":
            return [list(row) for row in self._conn.execute(
                """SELECT date, COALESCE(amount_decimal, CAST(amount AS TEXT)), category FROM tx_history
                   WHERE excluded_from_spend = 0 AND amount_currency = ? ORDER BY date ASC""", (currency,),
            ).fetchall()]
        if name == "subscription_rows":
            return [list(row) for row in self._conn.execute(
                """SELECT date, clean_name, COALESCE(amount_decimal, CAST(amount AS TEXT)), category,
                          COALESCE(original_description, '') FROM tx_history
                   WHERE excluded_from_spend = 0 AND amount_currency = ? ORDER BY date ASC""", (currency,),
            ).fetchall()]
        raise ValueError(f"Unknown ledger read model: {name}")

    def get_merchant_history(self) -> dict[str, list[tuple[str, float]]]:
        """Fetch all historical transactions grouped by merchant clean_name."""
        rows = self._conn.execute(
            """
            SELECT clean_name, date, amount
            FROM tx_history
            ORDER BY clean_name, date ASC
            """
        ).fetchall()
        
        history: dict[str, list[tuple[str, float]]] = {}
        for clean_name, date, amount in rows:
            history.setdefault(clean_name, []).append((date, amount))

        return history

    def list_transactions(
        self,
        *,
        page: int = 1,
        per_page: int = 50,
        category: str = "",
        uncategorized_only: bool = False,
        search: str = "",
        date_from: str = "",
        date_to: str = "",
        needs_review: bool | None = None,
        recurring: str = "",
        account_name: str = "",
        classification_source: str = "",
        transfer_status: str = "",
    ) -> dict[str, Any]:
        """Return paginated transaction results with SQL-backed filtering."""
        where: list[str] = []
        params: list[Any] = []

        if category:
            where.append("LOWER(category) = LOWER(?)")
            params.append(category)
        if uncategorized_only:
            where.append("category = 'Uncategorized'")
        if search:
            where.append(
                "(LOWER(clean_name) LIKE ? OR LOWER(COALESCE(original_description, '')) LIKE ? OR LOWER(COALESCE(counterpart, '')) LIKE ?)"
            )
            like = f"%{search.lower()}%"
            params.extend([like, like, like])
        if date_from:
            where.append("date >= ?")
            params.append(date_from)
        if date_to:
            where.append("date <= ?")
            params.append(date_to)
        if needs_review is not None:
            where.append("needs_review = ?")
            params.append(1 if needs_review else 0)
        if recurring:
            where.append("recurring = ?")
            params.append(recurring)
        if account_name:
            where.append("LOWER(account_name) = LOWER(?)")
            params.append(account_name)
        if classification_source:
            where.append("LOWER(classification_source) = LOWER(?)")
            params.append(classification_source)
        if transfer_status:
            where.append("transfer_status = ?")
            params.append(transfer_status)

        where_sql = f"WHERE {' AND '.join(where)}" if where else ""

        total_row = self._conn.execute(
            f"SELECT COUNT(*) FROM tx_history {where_sql}",
            params,
        ).fetchone()
        total = int(total_row[0] if total_row else 0)

        uncategorized_where = list(where)
        uncategorized_where.append("category = 'Uncategorized'")
        uncategorized_row = self._conn.execute(
            f"SELECT COUNT(*) FROM tx_history WHERE {' AND '.join(uncategorized_where)}",
            params,
        ).fetchone()
        uncategorized_total = int(uncategorized_row[0] if uncategorized_row else 0)

        offset = max(page - 1, 0) * per_page
        rows = self._conn.execute(
            f"""
            SELECT {_TX_HISTORY_SELECT_COLUMNS}
            FROM tx_history
            {where_sql}
            ORDER BY date DESC, tx_id DESC
            LIMIT ? OFFSET ?
            """,
            [*params, per_page, offset],
        ).fetchall()

        transactions = [self._row_to_transaction(row) for row in rows]
        return {
            "transactions": transactions,
            "total": total,
            "uncategorized_total": uncategorized_total,
            "page": page,
            "per_page": per_page,
            "pages": max(1, (total + per_page - 1) // per_page),
        }

    def list_all_transactions(
        self,
        *,
        category: str = "",
        uncategorized_only: bool = False,
        search: str = "",
        date_from: str = "",
        date_to: str = "",
        needs_review: bool | None = None,
        recurring: str = "",
        account_name: str = "",
        classification_source: str = "",
        transfer_status: str = "",
    ) -> list[dict[str, Any]]:
        """Return every transaction matching the provided filters."""
        first_page = self.list_transactions(
            page=1,
            per_page=1,
            category=category,
            uncategorized_only=uncategorized_only,
            search=search,
            date_from=date_from,
            date_to=date_to,
            needs_review=needs_review,
            recurring=recurring,
            account_name=account_name,
            classification_source=classification_source,
            transfer_status=transfer_status,
        )
        total = int(first_page["total"])
        if total == 0:
            return []
        return self.list_transactions(
            page=1,
            per_page=total,
            category=category,
            uncategorized_only=uncategorized_only,
            search=search,
            date_from=date_from,
            date_to=date_to,
            needs_review=needs_review,
            recurring=recurring,
            account_name=account_name,
            classification_source=classification_source,
            transfer_status=transfer_status,
        )["transactions"]

    def get_review_queue(
        self,
        *,
        page: int = 1,
        per_page: int = 50,
        reason: str = "",
        transfer_status: str = "",
        import_batch_id: str = "",
    ) -> dict[str, Any]:
        """Return paginated transactions that still need review."""
        where = ["needs_review = 1"]
        params: list[Any] = []

        if reason:
            where.append("review_reason = ?")
            params.append(reason)
        if transfer_status:
            where.append("transfer_status = ?")
            params.append(transfer_status)
        if import_batch_id:
            where.append("import_batch_id = ?")
            params.append(import_batch_id)

        where_sql = f"WHERE {' AND '.join(where)}"
        total_row = self._conn.execute(
            f"SELECT COUNT(*) FROM tx_history {where_sql}",
            params,
        ).fetchone()
        total = int(total_row[0] if total_row else 0)
        offset = max(page - 1, 0) * per_page
        rows = self._conn.execute(
            f"""
            SELECT {_TX_HISTORY_SELECT_COLUMNS}
            FROM tx_history
            {where_sql}
            ORDER BY date DESC, tx_id DESC
            LIMIT ? OFFSET ?
            """,
            [*params, per_page, offset],
        ).fetchall()
        items = [self._row_to_transaction(row) for row in rows]
        return {
            "items": items,
            "total": total,
            "page": page,
            "per_page": per_page,
            "pages": max(1, (total + per_page - 1) // per_page),
        }

    def list_all_review_queue(
        self,
        *,
        reason: str = "",
        transfer_status: str = "",
        import_batch_id: str = "",
    ) -> list[dict[str, Any]]:
        """Return every item that currently needs review."""
        first_page = self.get_review_queue(
            page=1,
            per_page=1,
            reason=reason,
            transfer_status=transfer_status,
            import_batch_id=import_batch_id,
        )
        total = int(first_page["total"])
        if total == 0:
            return []
        return self.get_review_queue(
            page=1,
            per_page=total,
            reason=reason,
            transfer_status=transfer_status,
            import_batch_id=import_batch_id,
        )["items"]

    def get_transaction(self, tx_id: str) -> dict[str, Any] | None:
        row = self._conn.execute(
            f"SELECT {_TX_HISTORY_SELECT_COLUMNS} FROM tx_history WHERE tx_id = ?",
            (tx_id,),
        ).fetchone()
        if not row:
            return None
        return self._row_to_transaction(row)

    def get_transactions_by_transfer_group(self, transfer_group_id: str) -> list[dict[str, Any]]:
        rows = self._conn.execute(
            f"""
            SELECT {_TX_HISTORY_SELECT_COLUMNS}
            FROM tx_history
            WHERE transfer_group_id = ?
            ORDER BY date DESC, tx_id DESC
            """,
            (transfer_group_id,),
        ).fetchall()
        return [self._row_to_transaction(row) for row in rows]

    def update_transaction(self, tx_id: str, **fields: Any) -> bool:
        """Update a single transaction row."""
        updates = {key: value for key, value in fields.items() if value is not None}
        if not updates:
            return False
        assignments = ", ".join(f"{column} = ?" for column in updates)
        params = list(updates.values()) + [tx_id]
        cur = self._conn.execute(
            f"UPDATE tx_history SET {assignments} WHERE tx_id = ?",
            params,
        )
        self._conn.commit()
        return cur.rowcount > 0

    def update_transactions(self, tx_ids: list[str], **fields: Any) -> int:
        """Update multiple transactions in one statement."""
        cleaned_ids = [str(tx_id) for tx_id in tx_ids if str(tx_id)]
        updates = {key: value for key, value in fields.items() if value is not None}
        if not cleaned_ids or not updates:
            return 0

        assignments = ", ".join(f"{column} = ?" for column in updates)
        placeholders = ",".join("?" for _ in cleaned_ids)
        params = list(updates.values()) + cleaned_ids
        cur = self._conn.execute(
            f"UPDATE tx_history SET {assignments} WHERE tx_id IN ({placeholders})",
            params,
        )
        self._conn.commit()
        return int(cur.rowcount)

    def find_transfer_candidates(
        self,
        *,
        tx_date: str,
        amount: float,
        currency: str | None = None,
        exclude_ids: list[str] | None = None,
    ) -> list[dict[str, Any]]:
        """Return nearby same-amount rows that could form a transfer pair."""
        from datetime import datetime, timedelta

        exclude_ids = exclude_ids or []
        ref_date = datetime.strptime(tx_date, "%Y-%m-%d").date()
        date_from = (ref_date - timedelta(days=3)).isoformat()
        date_to = (ref_date + timedelta(days=3)).isoformat()

        where = [
            "date >= ?",
            "date <= ?",
            "ABS(ABS(amount) - ?) <= 0.01",
            "transfer_status NOT IN ('confirmed', 'dismissed')",
            "excluded_from_spend = 0",
        ]
        params: list[Any] = [date_from, date_to, abs(amount)]
        if currency:
            where.append("amount_currency = ?")
            params.append(str(currency).upper())
        if exclude_ids:
            placeholders = ",".join("?" for _ in exclude_ids)
            where.append(f"tx_id NOT IN ({placeholders})")
            params.extend(exclude_ids)

        rows = self._conn.execute(
            f"""
            SELECT {_TX_HISTORY_SELECT_COLUMNS}
            FROM tx_history
            WHERE {' AND '.join(where)}
            ORDER BY date DESC, tx_id DESC
            """,
            params,
        ).fetchall()
        return [self._row_to_transaction(row) for row in rows]

    # ── Merchant Categories (for local mode) ──────────────────────

    def save_merchant_category(self, clean_name: str, category: str) -> None:
        """Save a merchant→category mapping for future local categorisation."""
        self._conn.execute(
            "INSERT OR REPLACE INTO merchant_categories (clean_name, category) VALUES (?, ?)",
            (clean_name, category),
        )
        self._conn.commit()

    def save_merchant_categories_batch(self, mappings: dict[str, str]) -> None:
        """Save multiple merchant→category mappings at once."""
        if not mappings:
            return
        self._conn.executemany(
            "INSERT OR REPLACE INTO merchant_categories (clean_name, category) VALUES (?, ?)",
            list(mappings.items()),
        )
        self._conn.commit()

    def get_merchant_categories(self) -> dict[str, str]:
        """Fetch all known merchant→category mappings."""
        rows = self._conn.execute("SELECT clean_name, category FROM merchant_categories").fetchall()
        return {name: cat for name, cat in rows}

    # ── Budget Limits ───────────────────────────────────────────

    def get_budget_limits(self, currency: str | None = None) -> dict[str, float]:
        """Return configured limits, optionally restricted to one denomination."""
        if currency:
            rows = self._conn.execute(
                """SELECT category, COALESCE(limit_decimal, CAST(monthly_limit AS TEXT))
                   FROM budget_limits WHERE limit_currency = ?""",
                (currency.upper(),),
            ).fetchall()
        else:
            rows = self._conn.execute(
                """SELECT category, COALESCE(limit_decimal, CAST(monthly_limit AS TEXT))
                   FROM budget_limits"""
            ).fetchall()
        return {str(category): float(as_decimal(amount)) for category, amount in rows}

    def get_budget_limits_exact(self, currency: str) -> dict[str, Any]:
        """Return exact Decimal budget values for one denomination."""
        rows = self._conn.execute(
            """SELECT category, COALESCE(limit_decimal, CAST(monthly_limit AS TEXT))
               FROM budget_limits WHERE limit_currency = ?""",
            (currency.upper(),),
        ).fetchall()
        return {str(category): as_decimal(amount) for category, amount in rows}

    def count_non_base_budget_limits(self, currency: str) -> int:
        row = self._conn.execute(
            """SELECT COUNT(*) FROM budget_limits
               WHERE limit_currency IS NULL OR limit_currency != ?""",
            (currency.upper(),),
        ).fetchone()
        return int(row[0] if row else 0)

    def save_budget_limit(
        self, category: str, monthly_limit: float, currency: str = "EUR",
    ) -> None:
        """Save an exact, denomination-scoped monthly budget limit."""
        code = str(currency).strip().upper()
        amount_text = decimal_text(monthly_limit, code)
        self._conn.execute(
            """INSERT OR REPLACE INTO budget_limits
               (category, monthly_limit, limit_currency, limit_decimal) VALUES (?, ?, ?, ?)""",
            (category, float(as_decimal(monthly_limit)), code, amount_text),
        )
        self._conn.commit()

    def get_app_setting(self, key: str, default: str | None = None) -> str | None:
        """Return a persisted app setting by key."""
        row = self._conn.execute(
            "SELECT value FROM app_settings WHERE key = ?",
            (key,),
        ).fetchone()
        return row[0] if row else default

    def set_app_setting(self, key: str, value: str) -> None:
        """Persist a simple app setting."""
        self._conn.execute(
            "INSERT OR REPLACE INTO app_settings (key, value) VALUES (?, ?)",
            (key, value),
        )
        self._conn.commit()

    def get_fx_rate(self, rate_date: str, base: str, quote: str) -> dict[str, Any] | None:
        row = self._conn.execute(
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
        self._conn.execute(
            """INSERT OR REPLACE INTO fx_rates
               (rate_date, base_currency, quote_currency, rate, effective_date, provider)
               VALUES (?, ?, ?, ?, ?, ?)""",
            (rate_date, base, quote, rate, effective_date, provider),
        )
        self._conn.commit()

    def find_legacy_transaction(self, tx_id: str) -> dict[str, Any] | None:
        """Look up old unscoped IDs while preserving account-aware re-imports."""
        return self.get_transaction(tx_id)

    def list_legacy_unknown_currency(self, *, page: int = 1, per_page: int = 50) -> dict[str, Any]:
        """List historical transactions whose amount denomination was never stored."""
        where = "amount_currency IS NULL AND currency_source = 'legacy-unknown'"
        total_row = self._conn.execute(
            f"SELECT COUNT(*) FROM tx_history WHERE {where}"
        ).fetchone()
        total = int(total_row[0] if total_row else 0)
        rows = self._conn.execute(
            f"""SELECT tx_id, date, clean_name, amount, category, account_name,
                       original_description, needs_review, review_reason
                FROM tx_history WHERE {where}
                ORDER BY date DESC, tx_id DESC LIMIT ? OFFSET ?""",
            (per_page, max(page - 1, 0) * per_page),
        ).fetchall()
        return {
            "items": [
                {
                    "id": str(row[0]), "date": str(row[1]), "merchant": str(row[2]),
                    "amount": float(row[3]), "category": str(row[4]),
                    "account_name": str(row[5] or ""),
                    "original_description": str(row[6] or ""),
                    "needs_review": bool(row[7]), "review_reason": str(row[8] or ""),
                }
                for row in rows
            ],
            "total": total,
            "page": page,
            "per_page": per_page,
            "pages": max(1, (total + per_page - 1) // per_page),
        }

    def confirm_legacy_currencies(self, confirmations: list[dict[str, Any]]) -> int:
        """Attach user-confirmed denominations without changing stored amounts."""
        import re

        if not confirmations:
            raise ValueError("At least one transaction currency confirmation is required")
        normalized: dict[str, tuple[str, str | None]] = {}
        for item in confirmations:
            tx_id = str(item.get("id") or "").strip()
            currency = str(item.get("currency") or "").strip().upper()
            if not tx_id or not re.fullmatch(r"[A-Z]{3}", currency):
                raise ValueError("Each item must include a transaction id and 3-letter currency code")
            if tx_id in normalized:
                raise ValueError("Transaction ids must be unique")
            amount_override = item.get("amount_decimal")
            amount_override = str(amount_override).strip() if amount_override is not None else None
            normalized[tx_id] = (currency, amount_override or None)

        with self._conn:
            placeholders = ",".join("?" for _ in normalized)
            rows = self._conn.execute(
                f"""SELECT tx_id, amount, review_reason FROM tx_history
                    WHERE tx_id IN ({placeholders}) AND amount_currency IS NULL
                      AND currency_source = 'legacy-unknown'""",
                list(normalized),
            ).fetchall()
            if len(rows) != len(normalized):
                raise ValueError("One or more transactions are no longer awaiting currency confirmation")
            for tx_id, amount, review_reason in rows:
                currency, amount_override = normalized[str(tx_id)]
                exact_amount = as_decimal(amount_override if amount_override is not None else amount)
                amount_text = decimal_text(exact_amount, currency)
                if as_decimal(amount_text) != exact_amount:
                    raise ValueError(
                        f"Transaction {tx_id} has precision not supported by {currency}; provide amount_decimal explicitly"
                    )
                is_currency_review = str(review_reason or "") == "legacy_currency_unknown"
                self._conn.execute(
                    """UPDATE tx_history SET amount = ?, amount_currency = ?, amount_decimal = ?,
                           amount_minor = ?, currency_source = 'user-confirmed',
                           needs_review = CASE WHEN ? THEN 0 ELSE needs_review END,
                           review_reason = CASE WHEN ? THEN '' ELSE review_reason END
                       WHERE tx_id = ?""",
                    (
                        float(amount_text), currency, amount_text, minor_units(amount_text, currency),
                        int(is_currency_review), int(is_currency_review), str(tx_id),
                    ),
                )
        return len(normalized)

    def list_accounts(self) -> list[dict[str, Any]]:
        rows = self._conn.execute(
            """SELECT account_id, display_name, currency, balance,
                      balance_decimal, balance_as_of, balance_source
               FROM accounts ORDER BY display_name, account_id"""
        ).fetchall()
        return [
            {
                "account_id": row[0], "name": row[1], "currency": row[2],
                "balance": float(row[3]) if row[3] is not None else None,
                "balance_decimal": row[4], "balance_as_of": row[5],
                "balance_source": row[6],
            }
            for row in rows
        ]

    # ── Category Rules ─────────────────────────────────────────

    def get_category_rules(self) -> list[dict[str, object]]:
        """Return all category rules sorted by priority then id."""
        rows = self._conn.execute(
            """
            SELECT id, rule_type, pattern, category, priority, is_active
            FROM category_rules
            ORDER BY priority ASC, id ASC
            """
        ).fetchall()
        return [
            {
                "id": int(rule_id),
                "rule_type": str(rule_type),
                "pattern": str(pattern),
                "category": str(category),
                "priority": int(priority),
                "is_active": bool(is_active),
            }
            for rule_id, rule_type, pattern, category, priority, is_active in rows
        ]

    def get_category_rule(self, rule_id: int) -> dict[str, object] | None:
        """Return a single category rule by id."""
        row = self._conn.execute(
            """
            SELECT id, rule_type, pattern, category, priority, is_active
            FROM category_rules
            WHERE id = ?
            """,
            (int(rule_id),),
        ).fetchone()
        if not row:
            return None

        return {
            "id": int(row[0]),
            "rule_type": str(row[1]),
            "pattern": str(row[2]),
            "category": str(row[3]),
            "priority": int(row[4]),
            "is_active": bool(row[5]),
        }

    def add_category_rule(self, rule_type: str, pattern: str, category: str) -> dict[str, object]:
        """Insert a new category rule and return it."""
        next_priority_row = self._conn.execute(
            "SELECT COALESCE(MAX(priority), 0) + 1 FROM category_rules"
        ).fetchone()
        priority = int(next_priority_row[0] if next_priority_row else 1)

        cur = self._conn.execute(
            """
            INSERT INTO category_rules (rule_type, pattern, category, priority, is_active)
            VALUES (?, ?, ?, ?, 1)
            """,
            (rule_type, pattern, category, priority),
        )
        self._conn.commit()
        return {
            "id": int(cur.lastrowid),
            "rule_type": rule_type,
            "pattern": pattern,
            "category": category,
            "priority": priority,
            "is_active": True,
        }

    def _normalize_rule_priorities(self) -> None:
        """Keep priorities contiguous after mutations."""
        rows = self._conn.execute(
            "SELECT id FROM category_rules ORDER BY priority ASC, id ASC"
        ).fetchall()
        for index, (rule_id,) in enumerate(rows, start=1):
            self._conn.execute(
                "UPDATE category_rules SET priority = ? WHERE id = ?",
                (index, int(rule_id)),
            )
        self._conn.commit()

    def move_category_rule(self, rule_id: int, direction: str) -> list[dict[str, object]]:
        """Move a category rule one step up or down in priority order."""
        normalized_direction = str(direction or "").strip().lower()
        if normalized_direction not in {"up", "down"}:
            raise ValueError("direction must be 'up' or 'down'")

        rules = self.get_category_rules()
        idx = next((i for i, rule in enumerate(rules) if int(rule["id"]) == int(rule_id)), None)
        if idx is None:
            raise KeyError(rule_id)

        target_idx = idx - 1 if normalized_direction == "up" else idx + 1
        if target_idx < 0 or target_idx >= len(rules):
            return rules

        rules[idx], rules[target_idx] = rules[target_idx], rules[idx]
        for priority, rule in enumerate(rules, start=1):
            self._conn.execute(
                "UPDATE category_rules SET priority = ? WHERE id = ?",
                (priority, int(rule["id"])),
            )
        self._conn.commit()
        return self.get_category_rules()

    def update_category_rule(
        self,
        rule_id: int,
        *,
        is_active: bool | None = None,
    ) -> dict[str, object] | None:
        """Update mutable category rule fields and return the fresh row."""
        updates: list[str] = []
        params: list[object] = []

        if is_active is not None:
            updates.append("is_active = ?")
            params.append(1 if is_active else 0)

        if not updates:
            return self.get_category_rule(rule_id)

        params.append(int(rule_id))
        cur = self._conn.execute(
            f"UPDATE category_rules SET {', '.join(updates)} WHERE id = ?",
            params,
        )
        self._conn.commit()
        if cur.rowcount <= 0:
            return None
        self._normalize_rule_priorities()
        return self.get_category_rule(rule_id)

    def delete_category_rule(self, rule_id: int) -> bool:
        """Delete a category rule by id. Returns True if deleted."""
        cur = self._conn.execute("DELETE FROM category_rules WHERE id = ?", (int(rule_id),))
        self._conn.commit()
        if cur.rowcount > 0:
            self._normalize_rule_priorities()
        return cur.rowcount > 0

    def get_training_data(self) -> list[dict[str, str]]:
        """Return structured local-classifier training rows.

        The ML module resolves precedence and weights between these sources:
        1. user overrides
        2. merchant memory
        3. tx history
        """
        examples: list[dict[str, str]] = []

        for original_description, category, clean_name in self._conn.execute(
            """
            SELECT original_description, category, clean_name
            FROM user_overrides
            WHERE category != ''
            """
        ).fetchall():
            if not original_description or not category:
                continue
            examples.append(
                {
                    "raw_description": str(original_description),
                    "clean_name": str(clean_name or ""),
                    "category": str(category),
                    "label_source": "user_override",
                }
            )

        for clean_name, category in self._conn.execute(
            """
            SELECT clean_name, category
            FROM merchant_categories
            WHERE category != ''
            """
        ).fetchall():
            if not clean_name or not category:
                continue
            examples.append(
                {
                    "raw_description": "",
                    "clean_name": str(clean_name),
                    "category": str(category),
                    "label_source": "merchant_memory",
                }
            )

        for original_description, clean_name, category in self._conn.execute(
            """
            SELECT original_description, clean_name, category
            FROM tx_history
            WHERE category != 'Uncategorized'
            """
        ).fetchall():
            if not category:
                continue
            if not original_description and not clean_name:
                continue
            examples.append(
                {
                    "raw_description": str(original_description or ""),
                    "clean_name": str(clean_name or ""),
                    "category": str(category),
                    "label_source": "tx_history",
                }
            )

        return examples

    # ── LLM Feedback Overrides ───────────────────────────────────

    def save_overrides(self, overrides: dict[str, dict[str, str]]) -> None:
        """Save a dictionary of user-defined overrides (original_description -> {category, clean_name})."""
        if not overrides:
            return
            
        rows_to_insert = [
            (orig_desc, data.get("category", ""), data.get("clean_name", ""))
            for orig_desc, data in overrides.items()
        ]
        
        self._conn.executemany(
            """
            INSERT OR REPLACE INTO user_overrides (original_description, category, clean_name)
            VALUES (?, ?, ?)
            """,
            rows_to_insert,
        )
        self._conn.commit()

    def get_overrides(self) -> dict[str, dict[str, str]]:
        """Fetch all manual overrides applied by the user in Google Sheets."""
        rows = self._conn.execute(
            """
            SELECT original_description, category, clean_name
            FROM user_overrides
            """
        ).fetchall()
        
        return {
            orig_desc: {"category": cat, "clean_name": name}
            for orig_desc, cat, name in rows
        }

    def record_learning_feedback(
        self,
        *,
        tx_id: str | None,
        original_description: str,
        clean_name: str,
        category: str,
        source: str,
        apply_to_future: bool,
    ) -> int:
        """Persist a user learning event for auditability and later review."""
        cur = self._conn.execute(
            """
            INSERT INTO learning_feedback (
                tx_id,
                original_description,
                clean_name,
                category,
                source,
                apply_to_future
            )
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (
                tx_id,
                original_description,
                clean_name,
                category,
                source,
                1 if apply_to_future else 0,
            ),
        )
        self._conn.commit()
        return int(cur.lastrowid)

    def get_recent_learning_feedback(self, limit: int = 40) -> list[dict[str, object]]:
        """Return the most recent user learning events."""
        rows = self._conn.execute(
            """
            SELECT id, tx_id, original_description, clean_name, category, source, apply_to_future, created_at
            FROM learning_feedback
            ORDER BY id DESC
            LIMIT ?
            """,
            (max(1, int(limit)),),
        ).fetchall()
        return [
            {
                "id": int(row_id),
                "tx_id": tx_id,
                "original_description": str(original_description),
                "clean_name": str(clean_name),
                "category": str(category),
                "source": str(source),
                "apply_to_future": bool(apply_to_future),
                "created_at": str(created_at),
            }
            for row_id, tx_id, original_description, clean_name, category, source, apply_to_future, created_at in rows
        ]

    def reapply_learning_to_history(self) -> dict[str, int]:
        """Re-apply overrides, rules, and merchant mappings to historical rows."""
        from spectra.rules import first_matching_rule

        overrides = self.get_overrides()
        rules = self.get_category_rules()
        merchant_categories = self.get_merchant_categories()
        rows = self._conn.execute(
            """
            SELECT tx_id, original_description, clean_name, category
            FROM tx_history
            ORDER BY date ASC, tx_id ASC
            """
        ).fetchall()

        updated = 0
        override_updates = 0
        rule_updates = 0
        merchant_updates = 0

        for tx_id, original_description, clean_name, current_category in rows:
            next_name = str(clean_name)
            next_category = str(current_category)
            applied_source = ""

            override = overrides.get(str(original_description or ""))
            if override:
                next_name = str(override.get("clean_name") or next_name)
                next_category = str(override.get("category") or next_category)
                applied_source = "override"
            else:
                matched_rule = first_matching_rule(
                    rules,
                    clean_name=str(clean_name),
                    raw_description=str(original_description or clean_name),
                )
                if matched_rule:
                    next_category = str(matched_rule["category"])
                    applied_source = "rule"
                elif str(clean_name) in merchant_categories:
                    next_category = str(merchant_categories[str(clean_name)])
                    applied_source = "merchant"

            if next_name == str(clean_name) and next_category == str(current_category):
                continue

            self._conn.execute(
                "UPDATE tx_history SET clean_name = ?, category = ? WHERE tx_id = ?",
                (next_name, next_category, str(tx_id)),
            )
            updated += 1
            if applied_source == "override":
                override_updates += 1
            elif applied_source == "rule":
                rule_updates += 1
            elif applied_source == "merchant":
                merchant_updates += 1

        self._conn.commit()
        return {
            "updated": updated,
            "override_updates": override_updates,
            "rule_updates": rule_updates,
            "merchant_updates": merchant_updates,
        }

    def count_review_queue(self) -> int:
        row = self._conn.execute(
            "SELECT COUNT(*) FROM tx_history WHERE needs_review = 1"
        ).fetchone()
        return int(row[0] if row else 0)

    def count_confirmed_transfer_groups(self) -> int:
        row = self._conn.execute(
            """
            SELECT COUNT(DISTINCT transfer_group_id)
            FROM tx_history
            WHERE transfer_status = 'confirmed' AND transfer_group_id != ''
            """
        ).fetchone()
        return int(row[0] if row else 0)

    def count(self) -> int:
        """Return total number of seen transactions."""
        row = self._conn.execute("SELECT COUNT(*) FROM seen_transactions").fetchone()
        return row[0] if row else 0

    def reset_all_data(self) -> dict[str, int]:
        """Delete all user data from the local DB while keeping schema intact."""
        tables = [
            "tx_history",
            "seen_transactions",
            "accounts",
            "merchant_categories",
            "user_overrides",
            "budget_limits",
            "learning_feedback",
        ]

        deleted_counts: dict[str, int] = {}
        for table in tables:
            row = self._conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()
            deleted_counts[table] = int(row[0] if row else 0)

        try:
            self._conn.execute("BEGIN")
            for table in tables:
                self._conn.execute(f"DELETE FROM {table}")
            self._conn.commit()
        except Exception:
            self._conn.rollback()
            raise

        # Reclaim free pages after mass delete.
        self._conn.execute("VACUUM")
        self._conn.commit()
        return deleted_counts

    # ── Housekeeping ─────────────────────────────────────────────

    def close(self) -> None:
        self._conn.close()

    def __enter__(self) -> "BookmarkDB":
        return self

    def __exit__(self, *exc) -> None:  # noqa: ANN002
        self.close()
