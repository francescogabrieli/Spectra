"""Universal CSV parser — reads bank export files from any institution."""

from __future__ import annotations

import csv
import logging
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

logger = logging.getLogger("spectra.csv_parser")


# ── Column name mappings ──────────────────────────────────────────
# Maps common bank column names → standard field names

_DATE_ALIASES = {
    "data", "date", "data operazione", "data contabile",
    "data valuta", "booking date", "transaction date",
    "data movimento", "data addebito", "data accredito",
    "data esecuzione", "datum",
}
_DESCRIPTION_ALIASES = {
    "descrizione", "description", "causale",
    "operazione",  # ISyBank primary description
    "details", "merchant", "commerciante", "motivo",
    "remittance", "transaction description", "narrative",
    "wording", "reference", "note", "notes", "memo",
    "causale pagamento", "descrizione operazione",
}
# Secondary/detail columns — merged into description for AI context
_DETAIL_ALIASES = {
    "dettagli",   # ISyBank — contains merchant name buried in a long string
    "description details", "transaction details", "detail", "additional info",
}
_COUNTERPART_ALIASES = {
    "counterparty", "counterpart", "counterparty name",
    "recipient", "recipient name",
    "payee", "payee name",
    "beneficiary", "beneficiary name", "beneficiario", "nome beneficiario",
    "destinatario", "nome destinatario",
    "ordinante", "mittente",
    "ragione sociale", "intestatario", "nome controparte",
}
_AMOUNT_ALIASES = {
    "importo", "amount", "valore", "value", "ammontare",
    "importo eur", "amount eur", "saldo", "totale",
    "dare/avere", "accredito/addebito", "entrate/uscite",
    "net amount",
}
_CREDIT_ALIASES = {
    "accredito", "credit", "entrate", "income", "in", "avere",
    "entrata", "versamento",
}
_DEBIT_ALIASES = {
    "addebito", "debit", "uscite", "expense", "out", "dare",
    "uscita", "prelievo", "pagamento",
}


@dataclass
class ParsedTransaction:
    """A single transaction parsed from a CSV row."""
    id: str         # hash of date+description+amount (for dedup)
    date: str       # YYYY-MM-DD
    amount: float   # negative = expense, positive = income
    currency: str
    raw_description: str
    original_amount: float | None = None
    original_currency: str | None = None
    counterpart: str = ""


def _normalize(s: str) -> str:
    """Lowercase, strip, remove extra spaces."""
    return re.sub(r"\s+", " ", s.strip().lower())


def _detect_delimiter(sample: str) -> str:
    """Detect CSV delimiter from a sample of the file."""
    counts = {
        ";": sample.count(";"),
        ",": sample.count(","),
        "\t": sample.count("\t"),
        "|": sample.count("|"),
    }
    return max(counts, key=lambda k: counts[k])


def _parse_amount(raw: str) -> float:
    """Parse an amount string handling Italian and English formats."""
    # Remove currency symbols and whitespace
    s = re.sub(r"[€$£\s]", "", raw.strip())
    # Strip leading '+' (some banks like ISyBank use +1.500,00 for credits)
    positive = s.startswith("+")
    s = s.lstrip("+")
    # Handle negative in parentheses: (100.00) → -100.00
    if s.startswith("(") and s.endswith(")"):
        s = "-" + s[1:-1]
    # Italian format: 1.234,56 → 1234.56
    if re.match(r"^-?\d{1,3}(\.\d{3})*(,\d+)?$", s):
        s = s.replace(".", "").replace(",", ".")
    # Remove thousands separator (English: 1,234.56)
    elif re.match(r"^-?\d{1,3}(,\d{3})*(\.\d+)?$", s):
        s = s.replace(",", "")
    # Simple comma as decimal: 1234,56
    else:
        s = s.replace(",", ".")
    try:
        result = float(s)
        return abs(result) if positive else result
    except ValueError:
        raise ValueError(f"Cannot parse amount: {raw!r}")


def _parse_date(raw: str) -> str:
    """Normalize date to YYYY-MM-DD from common formats."""
    from datetime import datetime
    s = raw.strip()

    # Try strptime formats (ordered most-specific first)
    fmt_list = [
        ("%Y-%m-%d", True),   # ISO: 2026-02-22
        ("%d/%m/%Y", False),  # EU: 22/02/2026
        ("%d-%m-%Y", False),  # EU: 22-02-2026
        ("%d.%m.%Y", False),  # DE: 22.02.2026
        ("%m/%d/%Y", False),  # US: 02/22/2026
        ("%m/%d/%y", False),  # US short: 1/31/26  ← ISyBank
        ("%d/%m/%y", False),  # EU short: 22/02/26
        ("%Y%m%d",  True),    # Compact: 20260222
    ]
    for fmt, _ in fmt_list:
        try:
            dt = datetime.strptime(s, fmt)
            # Sanity: reject obviously wrong years
            if dt.year < 2000 or dt.year > 2100:
                continue
            return dt.strftime("%Y-%m-%d")
        except ValueError:
            continue

    raise ValueError(f"Cannot parse date: {raw!r}")


def _make_id(date: str, description: str, amount: float) -> str:
    """Create a stable ID for dedup (hash of key fields)."""
    import hashlib
    key = f"{date}|{description.strip().lower()}|{amount:.2f}"
    return "CSV-" + hashlib.sha1(key.encode()).hexdigest()[:16]


_CURRENCY_ALIASES = {
    "valuta", "currency", "divisa", "moneta"
}

def _map_columns(headers: list[str]) -> dict[str, int]:
    """Find the indices of required columns based on known aliases."""
    mapping: dict[str, int] = {}
    for i, h_raw in enumerate(headers):
        h = h_raw.strip().lower()
        if h in _DATE_ALIASES:
            mapping.setdefault("date", i)
        if h in _DESCRIPTION_ALIASES:
            mapping.setdefault("description", i)
        if h in _DETAIL_ALIASES:
            mapping.setdefault("detail", i)   # secondary detail column
        if h in _COUNTERPART_ALIASES:
            mapping.setdefault("counterpart", i)
        if h in _AMOUNT_ALIASES:
            mapping.setdefault("amount", i)
        if h in _CREDIT_ALIASES:
            mapping.setdefault("credit", i)
        if h in _DEBIT_ALIASES:
            mapping.setdefault("debit", i)
        if h in _CURRENCY_ALIASES:
            mapping.setdefault("currency", i)

    return mapping


def _clean_description(text: str) -> str:
    """Remove verbose banking boilerplate and deduplicate substrings."""
    parts = [p.strip() for p in text.split("|")]
    cleaned_parts: list[str] = []

    for part in parts:
        # Strip generic POS boilerplate
        part = re.sub(r"(?i)\s*\beffettuato(?:\s+il\s+\d{2}[/\.]\d{2}[/\.]\d{4})?\s+(?:alle\s+ore\s+\d{4})?.*?\bpresso\s+", " ", part)
        part = re.sub(r"(?i)pagamento\s+effettuato\s+su\s+pos\s+estero", "", part)
        part = re.sub(r"(?i)pagamento\s+su\s+pos", "", part)
        part = re.sub(r"(?i)^pagamento\s+", "", part)
        
        # Strip verbose wiring (and just leave the recipient name)
        part = re.sub(r"(?i)^.*?bonifico\s+(?:istantaneo\s+)?da\s+(?:voi\s+)?disposto\s+a\s+favore\s+di\s*", "", part)
        part = re.sub(r"(?i)^.*?bonifico\s+(?:istantaneo\s+)?a\s+vostro\s+favore\s+disposto\s+da\s*", "", part)
        
        # Strip credit card masks / PAN (e.g., Carta n.5341 XXXX XXXX XX40, CARTA ... 4321, Carta n. 5341 XXXX XXXX 1234)
        part = re.sub(r"(?i)\bcarta(?:\s+n\.?)?\s*\d*\*+\d+(?:\s*[A-Z0-9X\*]+)*\b", "", part)
        part = re.sub(r"(?i)\bcarta(?:\s+n\.?)?\s*(?:\d{4}\s*)+(?:[x\*]{4}\s*)+(?:\d{4}|\w{4})\b", "", part)
        part = re.sub(r"(?i)\bcarta\s+\d+\*+\d+\b", "", part)
        part = re.sub(r"(?i)CARTA [A-Z0-9X\*]{10,}", "", part)
        
        # Strip banking / ATM codes (sometimes joined like XX40ABI 02008)
        part = re.sub(r"(?i)\s*ABI\s+\d+\b", " ", part)
        part = re.sub(r"(?i)CAB\s+\d+\b", " ", part)
        part = re.sub(r"(?i)ATM\s+\d+\b", " ", part)
        part = re.sub(r"(?i)\beffettuato\s+presso\b", "", part)
        part = re.sub(r"(?i)COD\.\s*\d+/?\d*\b", "", part)

        # Strip currency exchange boilerplate e.g. (ctv. Di 1081 Usd Al Cambio Di 0863334)
        part = re.sub(r"(?i)\(ctv\.\s+di\s+.*?\)", "", part)

        # Strip long alphanumeric trace IDs (like 02INTER...)
        part = re.sub(r"\b[A-Za-z0-9]{15,}\b", " ", part)
        
        part = re.sub(r"\s+", " ", part).strip()
        if part:
            cleaned_parts.append(part)

    # Deduplicate: if one part completely contains another, keep the longer one
    new_parts: list[str] = []
    for part in cleaned_parts:
        add_it = True
        for i, existing in enumerate(new_parts):
            if len(existing) > 5 and existing.lower() in part.lower():
                new_parts[i] = part
                add_it = False
                break
            if len(part) > 5 and part.lower() in existing.lower():
                add_it = False
                break
        if add_it:
            new_parts.append(part)

    return " | ".join(new_parts).strip(" |")


_COUNTERPART_NOISE_RE = re.compile(
    r"(?i)(?:"
    r"\beffettuato(?:\s+il\s+\d{2}[/\.]\d{2}[/\.]\d{2,4})?(?:\s+alle\s+ore\s+\d{3,4})?\b|"
    r"\bmediante\s+la\s+carta\b|"
    r"\bora\s+autorizzazione\b|"
    r"\bcarta(?:\s+n\.?)?\s*\d*[\*X]+[\d\*X\s]+\b|"
    r"\bABI\s+\d+\b|"
    r"\bCAB\s+\d+\b|"
    r"\bATM\s+\d+\b|"
    r"\bCOD\.?\s*\d+/?\d*\b|"
    r"\b[A-Z0-9]{15,}\b|"
    r"\(ctv\..*?\)|"
    r"\b\d{2}[/.]\d{2}[/.]\d{2,4}\b|"
    r"\b\d{4,}\b"
    r")"
)

_COUNTERPART_CAPTURE_PATTERNS = [
    re.compile(r"(?i)\bbonifico\s+(?:istantaneo\s+)?da\s+(?:voi\s+)?disposto\s+a\s+favore\s+di\s+(?P<value>.+)$"),
    re.compile(r"(?i)\bbonifico\s+(?:istantaneo\s+)?a\s+vostro\s+favo(?:re)?\s+disposto\s+da\s+(?P<value>.+)$"),
    re.compile(r"(?i)\b(?:beneficiary|beneficiario|recipient|payee|counterparty|counterpart|destinatario|ordinante|mittente)\s*[:\-]?\s+(?P<value>.+)$"),
    re.compile(r"(?i)\bpresso\s+(?P<value>.+)$"),
]


def _clean_counterpart(text: str) -> str:
    """Normalize a counterpart extracted from an explicit field or free text."""
    cleaned = str(text or "").strip()
    if not cleaned:
        return ""

    cleaned = _COUNTERPART_NOISE_RE.sub(" ", cleaned)
    cleaned = re.sub(r"(?i)\b(?:da\s+voi\s+disposto|a\s+vostro\s+favore\s+disposto\s+da)\b", " ", cleaned)
    cleaned = re.sub(r"\s*\|\s*", " ", cleaned)
    cleaned = re.sub(r"\s+", " ", cleaned).strip(" .|,;:-*()")
    if not cleaned:
        return ""

    try:
        from spectra.local_categorizer import _extract_merchant_name

        return _extract_merchant_name(cleaned)
    except Exception:
        return cleaned.title()


def _extract_counterpart(
    raw_description: str,
    detail: str = "",
    explicit_counterpart: str = "",
) -> str:
    """Resolve the best available counterpart signal with clear precedence."""
    explicit = _clean_counterpart(explicit_counterpart)
    if explicit:
        return explicit

    for text in (str(raw_description or "").strip(), str(detail or "").strip()):
        if not text:
            continue
        for pattern in _COUNTERPART_CAPTURE_PATTERNS:
            match = pattern.search(text)
            if not match:
                continue
            candidate = _clean_counterpart(match.group("value"))
            if candidate:
                return candidate

    return ""


def parse_csv(
    file_path: str | Path,
    currency: str = "EUR",
    encoding: str = "utf-8-sig",  # utf-8-sig handles BOM from Excel exports
) -> list[ParsedTransaction]:
    """Parse a bank CSV export into a list of ParsedTransaction objects.

    Supports any bank format — auto-detects:
    - Delimiter (comma, semicolon, tab, pipe)
    - Column names (Italian and English)
    - Amount format (Italian/English number format, debit/credit split)
    - Date format (ISO, European, compact)
    """
    path = Path(file_path)
    if not path.exists():
        raise FileNotFoundError(f"CSV file not found: {path}")

    # Read raw bytes to detect delimiter and encoding
    try:
        raw = path.read_text(encoding=encoding)
    except UnicodeDecodeError:
        raw = path.read_text(encoding="latin-1")

    delimiter = _detect_delimiter(raw[:2000])
    logger.info("Detected delimiter: %r for file: %s", delimiter, path.name)

    reader = csv.reader(raw.splitlines(), delimiter=delimiter)
    rows = list(reader)

    # Scan rows to find the real header: first row where at least 'date' maps
    header_idx = 0
    for i, row in enumerate(rows):
        col_candidate = _map_columns(row)
        if "date" in col_candidate:
            header_idx = i
            break
    else:
        # Fallback: first row with ≥3 non-empty cells
        for i, row in enumerate(rows):
            if len([c for c in row if c.strip()]) >= 3:
                header_idx = i
                break

    headers = rows[header_idx]
    data_rows = rows[header_idx + 1:]
    col = _map_columns(headers)

    logger.info(
        "Headers: %s → mapped: %s", headers, col
    )

    if "date" not in col:
        raise ValueError(
            f"Cannot find date column in CSV headers: {headers}\n"
            "Please check that your CSV has a date column."
        )
    if "description" not in col:
        raise ValueError(
            f"Cannot find description column in CSV headers: {headers}"
        )
    if "amount" not in col and ("credit" not in col and "debit" not in col):
        raise ValueError(
            f"Cannot find amount column in CSV headers: {headers}"
        )

    transactions: list[ParsedTransaction] = []
    skipped = 0

    for row_num, row in enumerate(data_rows, start=header_idx + 2):
        # Skip empty rows
        if not any(c.strip() for c in row):
            continue
        # Skip rows that are too short
        if len(row) <= max(col.values()):
            continue

        try:
            raw_date = row[col["date"]].strip()
            if not raw_date:
                continue

            date = _parse_date(raw_date)

            # Primary description + optional detail column (merged for AI context)
            primary_description = row[col["description"]].strip() if "description" in col else ""
            detail = ""
            if "detail" in col and col["detail"] < len(row):
                detail = row[col["detail"]].strip()
            explicit_counterpart = ""
            if "counterpart" in col and col["counterpart"] < len(row):
                explicit_counterpart = row[col["counterpart"]].strip()

            counterpart = _extract_counterpart(
                primary_description,
                detail=detail,
                explicit_counterpart=explicit_counterpart,
            )

            raw_desc = primary_description
            if detail and detail.lower() != primary_description.lower():
                raw_desc = f"{raw_desc} | {detail}"

            description = _clean_description(raw_desc)

            # Amount: either a single column or split credit/debit
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
                # Credits are positive, debits are negative
                amount = abs(credit) - abs(debit)

            tx_id = _make_id(date, description, amount)
            
            row_currency = currency
            if "currency" in col and col["currency"] < len(row):
                val = row[col["currency"]].strip().upper()
                if val:
                    row_currency = val

            transactions.append(
                ParsedTransaction(
                    id=tx_id,
                    date=date,
                    amount=amount,
                    currency=row_currency,
                    raw_description=description,
                    counterpart=counterpart,
                )
            )
        except (ValueError, IndexError) as e:
            logger.warning("Skipping row %d: %s — %s", row_num, row, e)
            skipped += 1

    logger.info(
        "Parsed %d transactions (%d skipped) from %s",
        len(transactions), skipped, path.name,
    )
    return transactions
