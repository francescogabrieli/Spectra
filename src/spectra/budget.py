"""Budget management — read/write budget tab, compute status per category."""

from __future__ import annotations

import logging
import re
from decimal import Decimal, InvalidOperation
from typing import Any

from spectra.money import as_decimal, quantize_money

logger = logging.getLogger("spectra.budget")

_BUDGET_SHEET = "Budget"
_HEADER_PREFIX = "Monthly Budget"


# ── Read ──────────────────────────────────────────────────────────

def _budget_currency(header: list[str]) -> str | None:
    """Infer the currency explicitly written in a budget column heading."""
    if len(header) < 2:
        return None
    match = re.search(r"\(([^()]+)\)", header[1])
    if not match:
        return None
    marker = match.group(1).strip().upper()
    # Existing releases named the EUR column with a symbol. Treat that
    # historical label as EUR, never as the current preference by default.
    if marker in {"€", "EUR"}:
        return "EUR"
    if marker in {"$", "USD"}:
        return "USD"
    if marker in {"£", "GBP"}:
        return "GBP"
    return marker if re.fullmatch(r"[A-Z]{3}", marker) else None


def read_budgets(sheets_client: Any, currency: str = "EUR") -> dict[str, Decimal]:
    """Read budgets only when their column explicitly matches *currency*."""
    try:
        ws = sheets_client._spreadsheet.worksheet(_BUDGET_SHEET)
    except Exception:
        return {}

    rows = ws.get_all_values()
    if len(rows) < 2:
        return {}

    sheet_currency = _budget_currency(rows[0])
    if sheet_currency != str(currency).upper():
        logger.info("Ignoring budget column in %s; it is %s, requested %s", _BUDGET_SHEET,
                    sheet_currency or "unknown currency", str(currency).upper())
        return {}

    budgets: dict[str, Decimal] = {}
    for row in rows[1:]:
        if len(row) < 2 or not row[0].strip():
            continue
        try:
            amount = as_decimal(row[1].replace(",", ".").strip() or "0")
            budgets[row[0].strip()] = amount
        except (ValueError, InvalidOperation):
            pass

    return budgets


# ── Write / sync ──────────────────────────────────────────────────

def sync_budget_sheet(sheets_client: Any, categories: list[str], currency: str = "EUR") -> None:
    """Create the Budget tab if missing, and add any new categories with 0 budget.

    The user fills in the actual numbers — Spectra never overwrites existing values.
    """
    try:
        ws = sheets_client._spreadsheet.worksheet(_BUDGET_SHEET)
        existing_rows = ws.get_all_values()
    except Exception:
        # Tab doesn't exist yet — create it
        ws = sheets_client._spreadsheet.add_worksheet(
            title=_BUDGET_SHEET, rows=100, cols=5
        )
        existing_rows = []
        logger.info("Created Budget tab")

    # Build set of categories already in the sheet
    existing_cats = {r[0].strip() for r in existing_rows[1:] if r}

    # Write header if missing
    if not existing_rows:
        ws.update("A1", [["Category", f"{_HEADER_PREFIX} ({str(currency).upper()})"]])
        _apply_budget_header_formatting(sheets_client, ws.id)

    # Append any new categories (budget = 0, user fills them in)
    new_rows = [[cat, 0] for cat in sorted(categories) if cat not in existing_cats]
    if new_rows:
        ws.append_rows(new_rows, value_input_option="USER_ENTERED")
        logger.info("Added %d new categories to Budget tab", len(new_rows))

    # Auto-resize columns
    try:
        from googleapiclient.discovery import build  # type: ignore[import-untyped]
        svc = build("sheets", "v4", credentials=sheets_client._creds)
        svc.spreadsheets().batchUpdate(
            spreadsheetId=sheets_client._spreadsheet_id,
            body={"requests": [{"autoResizeDimensions": {"dimensions": {
                "sheetId": ws.id, "dimension": "COLUMNS",
                "startIndex": 0, "endIndex": 3,
            }}}]},
        ).execute()
    except Exception:
        pass


def _apply_budget_header_formatting(sheets_client: Any, sheet_id: int) -> None:
    """Apply navy bold header to the Budget tab."""
    try:
        from googleapiclient.discovery import build  # type: ignore[import-untyped]
        svc = build("sheets", "v4", credentials=sheets_client._creds)
        navy = {"red": 0.07, "green": 0.12, "blue": 0.22}
        white = {"red": 1.0, "green": 1.0, "blue": 1.0}
        svc.spreadsheets().batchUpdate(
            spreadsheetId=sheets_client._spreadsheet_id,
            body={"requests": [
                {
                    "repeatCell": {
                        "range": {"sheetId": sheet_id, "startRowIndex": 0, "endRowIndex": 1},
                        "cell": {"userEnteredFormat": {
                            "backgroundColor": navy,
                            "textFormat": {"foregroundColor": white, "bold": True, "fontSize": 10},
                        }},
                        "fields": "userEnteredFormat(backgroundColor,textFormat)",
                    }
                },
                {
                    "updateSheetProperties": {
                        "properties": {"sheetId": sheet_id, "gridProperties": {"frozenRowCount": 1}},
                        "fields": "gridProperties.frozenRowCount",
                    }
                },
            ]},
        ).execute()
    except Exception as e:
        logger.warning("Budget header formatting failed: %s", e)


# ── Compute status ────────────────────────────────────────────────

def compute_budget_status(
    cat_totals: dict[str, float | Decimal],
    budgets: dict[str, float | Decimal],
    currency: str = "EUR",
) -> list[dict[str, Any]]:
    """Compute budget status for all tracked categories.

    Returns a list of dicts (one per category that has a budget > 0):
      { category, spent, budget, pct, status }
    where status is 'OK', 'Warning', or 'Over budget'.
    """
    results: list[dict[str, Any]] = []

    for cat, budget in sorted(budgets.items()):
        try:
            exact_budget = quantize_money(budget, currency)
            exact_spent = quantize_money(cat_totals.get(cat, 0), currency)
        except ValueError:
            continue
        if exact_budget <= 0:
            continue
        pct = round(float(exact_spent / exact_budget * Decimal(100)), 1)

        if pct >= 100:
            status = "🔴 Over budget"
        elif pct >= 80:
            status = "🟡 Warning"
        else:
            status = "🟢 OK"

        results.append({
            "category": cat,
            # Google Sheets is a floating-point external adapter. Core
            # SQLite/dashboard calculations keep Decimal values until here.
            "spent": float(exact_spent),
            "budget": float(exact_budget),
            "pct": pct,
            "status": status,
        })

    # Sort: over budget first, then warning, then OK
    order = {"🔴 Over budget": 0, "🟡 Warning": 1, "🟢 OK": 2}
    results.sort(key=lambda x: (order.get(x["status"], 9), -x["pct"]))

    return results
