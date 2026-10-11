"""YoY (Year-over-Year) trend computation and Trends tab management."""

from __future__ import annotations

import logging
from collections import defaultdict
from decimal import Decimal, InvalidOperation
from typing import Any

from spectra.money import as_decimal, quantize_money

logger = logging.getLogger("spectra.trends")

_TRENDS_SHEET = "Trends"
_MONTHS = ["Jan", "Feb", "Mar", "Apr", "May", "Jun",
           "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"]


# ── Data computation ──────────────────────────────────────────────

def _get_col_indices(rows: list[list[str]]) -> tuple[int, int, int] | None:
    """Return date, amount, and denomination columns, or None if not found."""
    header = [h.strip().lower() for h in rows[0]]
    try:
        return header.index("date"), header.index("amount"), header.index("currency")
    except ValueError:
        logger.warning("Could not find date/amount/currency columns")
        return None


def compute_monthly_data(
    rows: list[list[str]],
    base_currency: str = "EUR",
) -> dict[int, dict[int, dict[str, float]]]:
    """Compute monthly income, expenses, net cash flow and savings rate by year/month.

    Returns:
        { year: { month: { "income": float, "expenses": float, "net": float, "savings_rate": float } } }

    - income:       sum of positive amounts
    - expenses:     sum of absolute value of negative amounts
    - net:          income - expenses (positive = saved money)
    - savings_rate: net / income * 100  (None if income == 0)
    """
    if not rows:
        return {}

    cols = _get_col_indices(rows)
    if cols is None:
        return {}
    date_col, amount_col, currency_col = cols
    base_currency = str(base_currency).strip().upper()

    # Keep arithmetic exact through aggregation; this adapter converts to
    # Google Sheets' numeric representation only when preparing its output.
    income_acc: dict[int, dict[int, Decimal]] = defaultdict(lambda: defaultdict(Decimal))
    expense_acc: dict[int, dict[int, Decimal]] = defaultdict(lambda: defaultdict(Decimal))

    for row in rows[1:]:
        if len(row) <= max(date_col, amount_col, currency_col):
            continue
        if str(row[currency_col]).strip().upper() != base_currency:
            continue
        try:
            date_str = row[date_col].strip()
            year = int(date_str[:4])
            month = int(date_str[5:7])
            raw = row[amount_col].strip().replace(",", ".")
            amount = as_decimal(raw)
            if amount > 0:
                income_acc[year][month] += amount
            else:
                expense_acc[year][month] += abs(amount)
        except (ValueError, IndexError, InvalidOperation):
            continue

    # Build combined result
    all_years = sorted(set(income_acc) | set(expense_acc))
    result: dict[int, dict[int, dict[str, float]]] = {}
    for year in all_years:
        result[year] = {}
        all_months = sorted(set(income_acc.get(year, {})) | set(expense_acc.get(year, {})))
        for month in all_months:
            income = float(quantize_money(income_acc[year].get(month, Decimal(0)), base_currency))
            expenses = float(quantize_money(expense_acc[year].get(month, Decimal(0)), base_currency))
            net = float(quantize_money(
                income_acc[year].get(month, Decimal(0)) - expense_acc[year].get(month, Decimal(0)),
                base_currency,
            ))
            savings_rate = round(net / income * 100, 1) if income > 0 else 0.0
            result[year][month] = {
                "income": income,
                "expenses": expenses,
                "net": net,
                "savings_rate": savings_rate,
            }

    return result


def compute_yoy_delta(
    data: dict[int, dict[int, dict[str, float]]],
    field: str = "expenses",
) -> dict[int, dict[int, float | None]]:
    """Compute YoY % change for a given field (e.g., 'expenses', 'savings_rate')."""
    years = sorted(data.keys())
    deltas: dict[int, dict[int, float | None]] = {}

    for i in range(1, len(years)):
        curr_year = years[i]
        prev_year = years[i - 1]
        deltas[curr_year] = {}

        for month in range(1, 13):
            curr = data[curr_year].get(month, {}).get(field)
            prev = data[prev_year].get(month, {}).get(field)

            if curr is None or prev is None or prev == 0:
                deltas[curr_year][month] = None
            else:
                deltas[curr_year][month] = round((curr - prev) / abs(prev) * 100, 1)

    return deltas


# ── Sheet management ──────────────────────────────────────────────

def refresh_trends(sheets_client: Any, base_currency: str = "EUR") -> None:
    """Create or update the Trends tab with cash flow analysis and YoY deltas."""
    rows = sheets_client.get_all_rows()
    if len(rows) < 2:
        logger.info("Not enough data for trends yet")
        return

    currency = str(base_currency).strip().upper()
    data = compute_monthly_data(rows, currency)
    if not data:
        logger.info("No %s-denominated rows to build trends", currency)
        return

    years = sorted(data.keys())
    n_years = len(years)
    expense_deltas = compute_yoy_delta(data, "expenses")
    # ── Get or create Trends tab ─────────────────────────────────
    try:
        ws = sheets_client._spreadsheet.worksheet(_TRENDS_SHEET)
        ws.clear()
        logger.info("Cleared existing Trends tab")
    except Exception:
        ws = sheets_client._spreadsheet.add_worksheet(
            title=_TRENDS_SHEET, rows=60, cols=30
        )
        logger.info("Created Trends tab")

    def month_val(year: int, month: int, field: str) -> Any:
        return data[year].get(month, {}).get(field, "")

    # ── Table A: Monthly Expenses by Year (A1) ────────────────────
    hdr = ["Month"] + [str(y) for y in years]
    rows_a = [[m] + [month_val(y, i, "expenses") for y in years]
              for i, m in enumerate(_MONTHS, 1)]
    ws.update("A1", [hdr] + rows_a)

    # ── Table B: Monthly Income by Year (A15) ─────────────────────
    hdr_b = ["Month"] + [str(y) for y in years]
    rows_b = [[m] + [month_val(y, i, "income") for y in years]
              for i, m in enumerate(_MONTHS, 1)]
    ws.update("A15", [hdr_b] + rows_b)

    # ── Table C: Net Cash Flow (income - expenses) (A29) ──────────
    hdr_c = ["Month"] + [str(y) for y in years]
    rows_c = [[m] + [month_val(y, i, "net") for y in years]
              for i, m in enumerate(_MONTHS, 1)]
    ws.update("A29", [hdr_c] + rows_c)

    # ── Table D: Savings Rate % (A43) ─────────────────────────────
    hdr_d = ["Month"] + [str(y) for y in years]
    rows_d = [[m] + [month_val(y, i, "savings_rate") for y in years]
              for i, m in enumerate(_MONTHS, 1)]
    ws.update("A43", [hdr_d] + rows_d)

    # ── Table E: Expense YoY Delta % (A57) — appears from yr 2 ───
    if expense_deltas:
        delta_years = sorted(expense_deltas.keys())
        hdr_e = ["Month"] + [f"{y} vs {y-1} (Δ%)" for y in delta_years]
        rows_e = []
        for m_idx, m_name in enumerate(_MONTHS, 1):
            row = [m_name] + [
                expense_deltas[y].get(m_idx) if expense_deltas[y].get(m_idx) is not None else ""
                for y in delta_years
            ]
            rows_e.append(row)
        ws.update("A57", [hdr_e] + rows_e)

    logger.info(
        "Trends tab updated: %d year(s)", n_years
    )

    # ── Formatting ────────────────────────────────────────────────
    _format_trends_tab(sheets_client, ws.id, n_years, bool(expense_deltas))

    # ── Chart: Spending + Savings Rate ─────────────────────────────
    _create_trends_chart(sheets_client, ws.id, years, currency)


def _format_trends_tab(
    sheets_client: Any, sheet_id: int, n_years: int, has_delta: bool
) -> None:
    """Apply navy headers, green/red conditional formatting on net flow and delta tables."""
    try:
        from googleapiclient.discovery import build  # type: ignore[import-untyped]
        svc = build("sheets", "v4", credentials=sheets_client._creds)
        navy = {"red": 0.07, "green": 0.12, "blue": 0.22}
        white = {"red": 1.0, "green": 1.0, "blue": 1.0}
        n_cols = n_years + 1

        # Navy headers at rows: 1 (A1), 15 (B), 29 (C), 43 (D)
        header_row_indices = [0, 14, 28, 42]
        if has_delta:
            header_row_indices.append(56)

        requests: list[dict[str, Any]] = []
        for r in header_row_indices:
            requests.append({
                "repeatCell": {
                    "range": {"sheetId": sheet_id, "startRowIndex": r, "endRowIndex": r + 1,
                               "startColumnIndex": 0, "endColumnIndex": n_cols},
                    "cell": {"userEnteredFormat": {
                        "backgroundColor": navy,
                        "textFormat": {"foregroundColor": white, "bold": True, "fontSize": 10},
                    }},
                    "fields": "userEnteredFormat(backgroundColor,textFormat)",
                }
            })

        # Freeze row 1, auto-resize
        requests += [
            {
                "updateSheetProperties": {
                    "properties": {"sheetId": sheet_id, "gridProperties": {"frozenRowCount": 1}},
                    "fields": "gridProperties.frozenRowCount",
                }
            },
            {
                "autoResizeDimensions": {"dimensions": {
                    "sheetId": sheet_id, "dimension": "COLUMNS",
                    "startIndex": 0, "endIndex": n_cols + 1,
                }}
            },
        ]

        # Net cash flow table (Table C, rows 30–42 = indices 29–41):
        # Green = positive net (saved money), Red = negative
        for rule_val, bg, fg in [
            ("0", {"red": 0.85, "green": 0.96, "blue": 0.85}, {"red": 0.13, "green": 0.54, "blue": 0.13}),  # > 0 green
            ("0", {"red": 1.0, "green": 0.87, "blue": 0.87}, {"red": 0.76, "green": 0.15, "blue": 0.15}),   # < 0 red
        ]:
            condition_type = "NUMBER_GREATER" if bg["green"] > 0.9 else "NUMBER_LESS"
            requests.append({"addConditionalFormatRule": {"rule": {
                "ranges": [{"sheetId": sheet_id, "startRowIndex": 29, "endRowIndex": 42,
                            "startColumnIndex": 1, "endColumnIndex": n_cols}],
                "booleanRule": {
                    "condition": {"type": condition_type, "values": [{"userEnteredValue": rule_val}]},
                    "format": {"backgroundColor": bg, "textFormat": {"foregroundColor": fg}},
                },
            }, "index": 0}})

        # Expense delta table (Table E, rows 58+):
        # Green = spent LESS than prior year, Red = spent MORE
        if has_delta:
            requests.append({"addConditionalFormatRule": {"rule": {
                "ranges": [{"sheetId": sheet_id, "startRowIndex": 57, "endRowIndex": 70,
                            "startColumnIndex": 1, "endColumnIndex": n_cols}],
                "booleanRule": {
                    "condition": {"type": "NUMBER_LESS", "values": [{"userEnteredValue": "0"}]},
                    "format": {"backgroundColor": {"red": 0.85, "green": 0.96, "blue": 0.85},
                               "textFormat": {"foregroundColor": {"red": 0.13, "green": 0.54, "blue": 0.13}}},
                },
            }, "index": 0}})
            requests.append({"addConditionalFormatRule": {"rule": {
                "ranges": [{"sheetId": sheet_id, "startRowIndex": 57, "endRowIndex": 70,
                            "startColumnIndex": 1, "endColumnIndex": n_cols}],
                "booleanRule": {
                    "condition": {"type": "NUMBER_GREATER", "values": [{"userEnteredValue": "0"}]},
                    "format": {"backgroundColor": {"red": 1.0, "green": 0.87, "blue": 0.87},
                               "textFormat": {"foregroundColor": {"red": 0.76, "green": 0.15, "blue": 0.15}}},
                },
            }, "index": 0}})

        svc.spreadsheets().batchUpdate(
            spreadsheetId=sheets_client._spreadsheet_id,
            body={"requests": requests}
        ).execute()
        logger.info("Trends tab formatted")

    except Exception as e:
        logger.warning("Trends formatting failed: %s", e)


def _create_trends_chart(
    sheets_client: Any, sheet_id: int, years: list[int], currency: str = "EUR"
) -> None:
    """Create two line charts: Expenses by year + Savings Rate by year."""
    if len(years) < 1:
        return
    try:
        from googleapiclient.discovery import build  # type: ignore[import-untyped]
        svc = build("sheets", "v4", credentials=sheets_client._creds)
        spreadsheet_id = sheets_client._spreadsheet_id

        # Delete existing charts on this sheet
        meta = svc.spreadsheets().get(spreadsheetId=spreadsheet_id).execute()
        del_requests = [
            {"deleteEmbeddedObject": {"objectId": c["chartId"]}}
            for sheet in meta.get("sheets", [])
            if sheet["properties"]["sheetId"] == sheet_id
            for c in sheet.get("charts", [])
        ]

        n_years = len(years)
        anchor_col = n_years + 2  # Place charts to the right of data

        def line_chart(title: str, data_start_row: int, anchor_row: int) -> dict[str, Any]:
            series = [
                {
                    "series": {"sourceRange": {"sources": [{
                        "sheetId": sheet_id,
                        "startRowIndex": data_start_row, "endRowIndex": data_start_row + 13,
                        "startColumnIndex": i + 1, "endColumnIndex": i + 2,
                    }]}},
                    "targetAxis": "LEFT_AXIS",
                }
                for i in range(n_years)
            ]
            return {
                "addChart": {
                    "chart": {
                        "spec": {
                            "title": title,
                            "basicChart": {
                                "chartType": "LINE",
                                "legendPosition": "BOTTOM_LEGEND",
                                "axis": [
                                    {"position": "BOTTOM_AXIS", "title": "Month"},
                                    {"position": "LEFT_AXIS", "title": currency},
                                ],
                                "domains": [{"domain": {"sourceRange": {"sources": [{
                                    "sheetId": sheet_id,
                                    "startRowIndex": data_start_row, "endRowIndex": data_start_row + 13,
                                    "startColumnIndex": 0, "endColumnIndex": 1,
                                }]}}}],
                                "series": series,
                                "headerCount": 1,
                            }
                        },
                        "position": {
                            "overlayPosition": {
                                "anchorCell": {"sheetId": sheet_id, "rowIndex": anchor_row, "columnIndex": anchor_col},
                                "widthPixels": 480,
                                "heightPixels": 280,
                            }
                        }
                    }
                }
            }

        add_requests = [
            line_chart("Monthly Expenses by Year", 0, 0),        # Table A, placed at top-right
            line_chart("Monthly Net Cash Flow by Year", 28, 20), # Table C
        ]

        svc.spreadsheets().batchUpdate(
            spreadsheetId=spreadsheet_id,
            body={"requests": del_requests + add_requests}
        ).execute()
        logger.info("Trends charts created/updated")

    except Exception as e:
        logger.warning("Trends chart failed: %s", e)
