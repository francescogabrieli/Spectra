"""SQLite bookmark — tracks which transactions have already been imported."""

from __future__ import annotations

import logging
import sqlite3
from pathlib import Path
from typing import Any

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
    original_currency TEXT
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
    monthly_limit REAL NOT NULL
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
original_currency
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
        # budget_limits table (for existing DBs pre-budget feature)
        self._conn.execute("""
            CREATE TABLE IF NOT EXISTS budget_limits (
                category      TEXT PRIMARY KEY,
                monthly_limit REAL NOT NULL
            )
        """)
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

    def save_history(self, transactions: list[Any]) -> None:
        """Save a batch of parsed and ML-categorised transactions to history."""
        self._conn.executemany(
            """
            INSERT OR REPLACE INTO tx_history (
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
                original_currency
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            [
                (
                    t.id,
                    t.date,
                    t.clean_name,
                    t.amount,
                    getattr(t, "category", "Uncategorized"),
                    getattr(t, "original_description", ""),
                    getattr(t, "counterpart", ""),
                    getattr(t, "recurring", ""),
                    getattr(t, "classification_source", ""),
                    getattr(t, "category_confidence", None),
                    1 if getattr(t, "needs_review", False) else 0,
                    getattr(t, "review_reason", ""),
                    getattr(t, "account_name", ""),
                    getattr(t, "import_batch_id", ""),
                    getattr(t, "transfer_group_id", ""),
                    getattr(t, "transfer_status", "none") or "none",
                    1 if getattr(t, "excluded_from_spend", False) else 0,
                    getattr(t, "original_amount", None),
                    getattr(t, "original_currency", None),
                )
                for t in transactions
            ],
        )
        # Also mark them as seen
        self.mark_seen_batch([t.id for t in transactions])

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

    def get_budget_limits(self) -> dict[str, float]:
        """Return all category→monthly_limit mappings."""
        rows = self._conn.execute("SELECT category, monthly_limit FROM budget_limits").fetchall()
        return {cat: lim for cat, lim in rows}

    def save_budget_limit(self, category: str, monthly_limit: float) -> None:
        """Save or update the monthly budget limit for a category."""
        self._conn.execute(
            "INSERT OR REPLACE INTO budget_limits (category, monthly_limit) VALUES (?, ?)",
            (category, monthly_limit),
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
