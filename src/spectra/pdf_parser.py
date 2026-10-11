"""Universal PDF parser — extracts transactions from bank statement PDFs."""

from __future__ import annotations

import logging
import hashlib
import re
from pathlib import Path

from spectra.csv_parser import (
    ParsedTransaction,
    _make_id,
    _map_columns,
    _parse_amount,
    _parse_date,
    _safe_account_label,
)
from spectra.money import decimal_text

logger = logging.getLogger("spectra.pdf_parser")


def parse_pdf(
    file_path: str | Path,
    currency: str = "EUR",
) -> list[ParsedTransaction]:
    """Extract transactions from a bank PDF statement.

    Strategy:
    1. Try pdfplumber table extraction (works on most bank PDFs with clean tables)
    2. Fall back to line-by-line regex parsing (for text-based PDFs)
    """
    try:
        import pdfplumber  # type: ignore[import-untyped]
    except ImportError:
        raise ImportError(
            "pdfplumber is required for PDF support. Install it with:\n"
            "  pip install pdfplumber"
        )
    try:
        from pypdf import PdfReader  # type: ignore[import-untyped]
    except ImportError:
        raise ImportError(
            "pypdf is required for PDF text extraction fallback. Install it with:\n"
            "  pip install pypdf"
        )

    path = Path(file_path)
    if not path.exists():
        raise FileNotFoundError(f"PDF file not found: {path}")

    logger.info("Parsing PDF statement")

    with pdfplumber.open(path) as pdf:
        # ── Strategy 1: Table extraction ─────────────────────────
        table_transactions = _extract_from_tables(pdf, currency)

        # ── Strategy 2: Text-based regex extraction ───────────────
        text_transactions = _extract_from_text_with_pypdf(path, PdfReader, currency)

        if not table_transactions and not text_transactions:
            return []

        if table_transactions and text_transactions:
            # Prefer the source that produced the most complete statement.
            # Some PDFs expose a clean table, while others only parse reliably
            # through text extraction when rows are wrapped across lines.
            if len(text_transactions) >= len(table_transactions):
                chosen = text_transactions
                source = "text"
            else:
                chosen = table_transactions
                source = "table"
            logger.info(
                "PDF extraction: %d table + %d text transactions; using %s (%d rows)",
                len(table_transactions),
                len(text_transactions),
                source,
                len(chosen),
            )
            return chosen
        elif table_transactions:
            logger.info(
                "Table extraction: %d transactions",
                len(table_transactions),
            )
            return table_transactions
        else:
            logger.info(
                "Text extraction: %d transactions",
                len(text_transactions),
            )
            return text_transactions


# ── Strategy 1: Table extraction ─────────────────────────────────


def _extract_from_tables(pdf: Any, currency: str) -> list[ParsedTransaction]:
    """Try to extract transaction tables from each PDF page."""
    all_rows: list[list[str]] = []
    header: list[str] | None = None

    for page in pdf.pages:
        tables = page.extract_tables()
        for table in tables:
            if not table or len(table) < 2:
                continue

            # Find the header row (first row with recognizable column names)
            potential_header = [str(c or "").strip() for c in table[0]]
            col_map = _map_columns(potential_header)

            if "date" in col_map and ("amount" in col_map or "credit" in col_map or "debit" in col_map):
                if header is None:
                    header = potential_header
                # Add data rows (skip header row of subsequent tables)
                data_start = 1 if header == potential_header or not all_rows else 0
                for row in table[data_start:]:
                    all_rows.append([str(c or "").strip() for c in row])

    if not all_rows or header is None:
        return []

    return _rows_to_transactions(header, all_rows, currency)


def _rows_to_transactions(
    header: list[str],
    rows: list[list[str]],
    currency: str,
) -> list[ParsedTransaction]:
    """Convert table rows to ParsedTransaction objects using column mapping."""
    col = _map_columns(header)
    transactions: list[ParsedTransaction] = []
    skipped = 0
    occurrences: dict[tuple[str, str, float, str, str], int] = {}

    for row in rows:
        if not any(c.strip() for c in row):
            continue
        if len(row) <= max(col.values()):
            continue

        try:
            raw_date = row[col["date"]].strip()
            if not raw_date:
                continue
            date = _parse_date(raw_date)
            description = row[col.get("description", -1)].strip() if "description" in col else ""

            if "amount" in col:
                raw_amount = row[col["amount"]].strip()
                if not raw_amount:
                    continue
                amount = _parse_amount(raw_amount)
            else:
                raw_credit = row[col["credit"]].strip() if "credit" in col else ""
                raw_debit = row[col["debit"]].strip() if "debit" in col else ""
                credit = _parse_amount(raw_credit) if raw_credit else 0.0
                debit = _parse_amount(raw_debit) if raw_debit else 0.0
                amount = abs(credit) - abs(debit)

            statement_category = row[col["category"]].strip() if "category" in col else ""
            statement_category = statement_category.replace("€", "").strip()

            row_currency = row[col["currency"]].strip().upper() if "currency" in col and col["currency"] < len(row) else currency.upper()
            account_identity = row[col["account"]].strip() if "account" in col and col["account"] < len(row) else ""
            account_name = _safe_account_label(account_identity)
            account_id = (
                "acct-" + hashlib.sha256(
                    f"{account_identity.casefold()}|{row_currency.upper()}".encode("utf-8")
                ).hexdigest()[:20]
                if account_identity else ""
            )
            fingerprint = (date, description.casefold(), round(float(amount), 2), row_currency, account_identity.casefold())
            occurrence = occurrences.get(fingerprint, 0)
            occurrences[fingerprint] = occurrence + 1

            transactions.append(ParsedTransaction(
                id=_make_id(
                    date, description, amount, account_name=account_identity,
                    currency=row_currency, occurrence=occurrence,
                ),
                date=date,
                amount=amount,
                currency=row_currency,
                raw_description=description,
                amount_decimal=decimal_text(amount, row_currency),
                statement_category=statement_category,
                account_name=account_name,
                account_id=account_id,
                legacy_id=_make_id(date, description, amount),
            ))
        except (ValueError, IndexError):
            skipped += 1
            logger.debug("Skipping malformed PDF table row")

    if skipped:
        logger.warning("Skipped %d malformed rows", skipped)

    return transactions


# ── Strategy 2: Text-based regex extraction ───────────────────────


# Pattern that matches a line starting with a date, then description, then amount(s)
# Handles most Italian/European bank statement formats
_TX_PATTERN = re.compile(
    r"(?P<date>\d{1,2}[/\-\.]\d{1,2}[/\-\.]\d{2,4}|\d{4}[/\-\.]\d{2}[/\-\.]\d{2})"  # date
    r"\s+"
    r"(?P<desc>.+?)"  # description (lazy)
    r"\s+"
    r"(?P<amount>[+\-]?\s*[\d\.,]+)"  # amount
    r"\s*(?P<currency>[A-Z]{3})?\s*$",  # optional currency
    re.IGNORECASE,
)

_BANK_TX_PATTERN = re.compile(
    r"(?P<date>\d{1,2}[/\-\.]\d{1,2}[/\-\.]\d{2,4}|\d{4}[/\-\.]\d{2}[/\-\.]\d{2})"
    r"\s+"
    r"(?P<desc>.+?)"
    r"\s+(?P<posted>SI|NO)"
    r"\s+"
    r"(?P<category>.+?)"
    r"\s+"
    r"(?P<amount>[+\-]?\s*[\d\.,]+)"
    r"\s*(?P<currency>[A-Z]{3})?\s*$",
    re.IGNORECASE,
)

_TX_START_RE = re.compile(
    r"^\s*(?:\d{1,2}[/\-\.]\d{1,2}[/\-\.]\d{2,4}|\d{4}[/\-\.]\d{2}[/\-\.]\d{2})\b"
)


def _chunk_transaction_lines(lines: list[str]) -> list[str]:
    """Join wrapped PDF lines into transaction-sized chunks.

    Many bank PDFs split a single transaction over multiple visual lines.
    We keep appending to the current chunk until the next line starts with a date.
    """
    chunks: list[str] = []
    current: list[str] = []

    for raw_line in lines:
        line = raw_line.strip()
        if not line:
            continue

        if _TX_START_RE.match(line):
            if current:
                chunks.append(" ".join(current))
            current = [line]
            continue

        if current:
            existing = " ".join(current)
            if _BANK_TX_PATTERN.match(existing) or _TX_PATTERN.match(existing):
                # Ignore footer/header noise once the transaction line is already complete.
                continue
            current.append(line)

    if current:
        chunks.append(" ".join(current))

    return chunks


def _extract_from_text_with_pypdf(
    file_path: Path,
    pdf_reader_cls: Any,
    currency: str,
) -> list[ParsedTransaction]:
    """Regex-based fallback: scan each line for date + description + amount."""
    transactions: list[ParsedTransaction] = []
    occurrences: dict[tuple[str, str, str], int] = {}
    reader = pdf_reader_cls(str(file_path))

    for page in reader.pages:
        text = page.extract_text() or ""
        for chunk in _chunk_transaction_lines(text.splitlines()):
            m = _BANK_TX_PATTERN.match(chunk) or _TX_PATTERN.match(chunk)
            if not m:
                continue

            try:
                date = _parse_date(m.group("date"))
                description = m.group("desc").strip()
                amount = _parse_amount(m.group("amount").replace(" ", ""))
                currency_code = str(m.groupdict().get("currency") or currency).strip().upper()
                fingerprint = (date, description.casefold(), decimal_text(amount, currency_code))
                occurrence = occurrences.get(fingerprint, 0)
                occurrences[fingerprint] = occurrence + 1

                transactions.append(ParsedTransaction(
                    id=_make_id(
                        date, description, amount, currency=currency_code,
                        occurrence=occurrence,
                    ),
                    date=date,
                    amount=amount,
                    currency=currency_code,
                    raw_description=description,
                    amount_decimal=decimal_text(amount, currency_code),
                    legacy_id=_make_id(date, description, amount),
                    statement_category=(
                        m.group("category").replace("€", "").strip()
                        if "category" in m.groupdict()
                        else ""
                    ),
                ))
            except ValueError:
                continue

    return transactions


# Type alias used above (avoid circular import with typing)
from typing import Any  # noqa: E402
