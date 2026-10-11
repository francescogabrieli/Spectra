"""Dashboard — builds a summary tab with 3 charts + recurring cash flow in Google Sheets."""

from __future__ import annotations

import logging
from collections import defaultdict
from datetime import datetime
from decimal import Decimal, InvalidOperation
from typing import Any

from spectra.money import as_decimal, quantize_money

logger = logging.getLogger("spectra.dashboard")


def _summarize_rows(
    all_rows: list[list[str]], base_currency: str,
) -> tuple[dict[str, Decimal], dict[str, dict[str, Decimal]], dict[str, dict[str, Any]], int]:
    """Aggregate only rows whose saved amount denomination matches the base."""
    if len(all_rows) < 2:
        return {}, {}, {}, 0
    header = [str(value).strip().casefold() for value in all_rows[0]]
    try:
        date_idx = header.index("date")
        cat_idx = header.index("category")
        amt_idx = header.index("amount")
        currency_idx = header.index("currency")
    except ValueError:
        logger.warning("Required Date, Category, Amount, or Currency columns not found")
        return {}, {}, {}, 0

    merch_idx = header.index("merchant") if "merchant" in header else None
    recur_idx = header.index("recurring") if "recurring" in header else None
    base_currency = str(base_currency).strip().upper()
    cat_totals: dict[str, Decimal] = defaultdict(Decimal)
    monthly: dict[str, dict[str, Decimal]] = defaultdict(
        lambda: {"income": Decimal(0), "expenses": Decimal(0)}
    )
    recurring_items: dict[str, dict[str, Any]] = {}
    included = 0

    for row in all_rows[1:]:
        if len(row) <= max(date_idx, cat_idx, amt_idx, currency_idx):
            continue
        # Empty and other-currency rows are not comparable. In particular,
        # FX-failed transactions retain their source currency and stay out.
        if str(row[currency_idx]).strip().upper() != base_currency:
            continue
        try:
            date_str = str(row[date_idx]).strip()
            category = str(row[cat_idx]).strip() or "Other"
            amount = as_decimal(str(row[amt_idx]).strip().replace(",", "."))
            month = datetime.strptime(date_str, "%Y-%m-%d").strftime("%Y-%m")
        except (ValueError, IndexError, InvalidOperation):
            continue
        included += 1
        if amount < 0:
            cat_totals[category] += abs(amount)
        if amount >= 0:
            monthly[month]["income"] += amount
        else:
            monthly[month]["expenses"] += abs(amount)

        if recur_idx is not None and recur_idx < len(row):
            recur_type = str(row[recur_idx]).strip()
            if recur_type:
                merchant = (
                    str(row[merch_idx]).strip()
                    if merch_idx is not None and merch_idx < len(row)
                    else category
                )
                key = merchant.lower()
                if key not in recurring_items:
                    recurring_items[key] = {
                        "name": merchant, "type": recur_type,
                        "total": Decimal(0), "count": 0,
                    }
                recurring_items[key]["total"] += amount
                recurring_items[key]["count"] += 1
    return cat_totals, monthly, recurring_items, included


def refresh_dashboard(sheets_client: Any, base_currency: str = "EUR") -> None:
    """Read Transactions sheet → compute summaries → write Dashboard tab → create charts."""

    # ── 1. Read all transactions ──────────────────────────────────
    all_rows = sheets_client.get_all_rows()
    if len(all_rows) < 2:
        logger.info("No data to build dashboard")
        return

    # ── 2. Compute summaries in one explicit denomination ──────────
    currency = str(base_currency).strip().upper()
    cat_totals, monthly, recurring_items, included = _summarize_rows(all_rows, currency)
    if not included:
        logger.info("No %s-denominated rows to build dashboard", currency)
        return
    sorted_cats = sorted(cat_totals.items(), key=lambda x: x[1], reverse=True)
    sorted_months = sorted(monthly.keys())[-6:]

    # ── 3. Get or create Dashboard worksheet ──────────────────────
    ss = sheets_client._spreadsheet
    try:
        ws = ss.worksheet("Dashboard")
        ws.clear()
        logger.info("Cleared existing Dashboard tab")
    except Exception:
        ws = ss.add_worksheet(title="Dashboard", rows=200, cols=20)
        logger.info("Created Dashboard tab")

    # ── 4. Write summary tables ───────────────────────────────────

    # Table A: Spending by Category (A1)
    cat_header = [["Category", f"Total Spent ({currency})"]]
    cat_rows = [[name, float(quantize_money(total, currency))] for name, total in sorted_cats]
    ws.update("A1", cat_header + cat_rows)

    # Table B: Monthly summary (D1)
    month_header = [["Month", f"Income ({currency})", f"Expenses ({currency})", f"Net ({currency})"]]
    month_rows = [
        [m, float(quantize_money(monthly[m]["income"], currency)),
         float(quantize_money(monthly[m]["expenses"], currency)),
         float(quantize_money(monthly[m]["income"] - monthly[m]["expenses"], currency))]
        for m in sorted_months
    ]
    ws.update("D1", month_header + month_rows)

    # Table C: Recurring Cash Flow (I1)
    recur_rows: list[list[Any]] = []
    if recurring_items:
        subs = sorted(
            [v for v in recurring_items.values() if v["type"] == "Subscription"],
            key=lambda x: x["total"],
        )
        income = sorted(
            [v for v in recurring_items.values() if v["type"] == "Salary/Income"],
            key=lambda x: x["total"], reverse=True,
        )

        recur_header = [["Recurring Cash Flow", "Type", f"Amount ({currency})"]]
        for item in subs + income:
            avg = float(quantize_money(item["total"] / max(item["count"], 1), currency))
            recur_rows.append([item["name"], item["type"], avg])

        sub_total = float(quantize_money(sum(
            (v["total"] / max(v["count"], 1) for v in subs), Decimal(0)
        ), currency))
        inc_total = float(quantize_money(sum(
            (v["total"] / max(v["count"], 1) for v in income), Decimal(0)
        ), currency))
        recur_rows.append(["", "", ""])
        recur_rows.append(["Total Subscriptions/mo", "", sub_total])
        recur_rows.append(["Total Recurring Income/mo", "", inc_total])

        ws.update("I1", recur_header + recur_rows)

    # Table D: Budget Status (L1)
    from spectra.budget import read_budgets, sync_budget_sheet, compute_budget_status
    sync_budget_sheet(sheets_client, [cat for cat, _ in sorted_cats], currency)
    budgets = read_budgets(sheets_client, currency)
    budget_status = compute_budget_status(dict(sorted_cats), budgets, currency)

    budget_header = [["Category", f"Spent ({currency})", f"Budget ({currency})", "% Used", "Status"]]
    budget_rows = [
        [b["category"], b["spent"], b["budget"], b["pct"], b["status"]]
        for b in budget_status
    ]
    if budget_rows:
        ws.update("L1", budget_header + budget_rows)

    logger.info(
        "Dashboard data written: %d categories, %d months, %d recurring items, %d budget entries",
        len(sorted_cats), len(sorted_months), len(recurring_items), len(budget_rows),
    )

    # ── 4b. Format ALL Dashboard headers ──────────────────────────
    try:
        from googleapiclient.discovery import build  # type: ignore[import-untyped]
        _svc = build("sheets", "v4", credentials=sheets_client._creds)
        _meta = _svc.spreadsheets().get(spreadsheetId=sheets_client._spreadsheet_id).execute()
        _dash_id = next(
            (s["properties"]["sheetId"] for s in _meta["sheets"]
             if s["properties"]["title"] == "Dashboard"), None
        )
        if _dash_id is not None:
            _navy = {"red": 0.07, "green": 0.12, "blue": 0.22}
            _white = {"red": 1.0, "green": 1.0, "blue": 1.0}
            _header_ranges = [
                {"sheetId": _dash_id, "startRowIndex": 0, "endRowIndex": 1, "startColumnIndex": 0, "endColumnIndex": 2},   # A1:B1
                {"sheetId": _dash_id, "startRowIndex": 0, "endRowIndex": 1, "startColumnIndex": 3, "endColumnIndex": 7},   # D1:G1
                {"sheetId": _dash_id, "startRowIndex": 0, "endRowIndex": 1, "startColumnIndex": 8, "endColumnIndex": 11},  # I1:K1
                {"sheetId": _dash_id, "startRowIndex": 0, "endRowIndex": 1, "startColumnIndex": 11, "endColumnIndex": 16}, # L1:P1 (Budget)
            ]
            _fmt_requests = [
                {
                    "repeatCell": {
                        "range": r,
                        "cell": {"userEnteredFormat": {
                            "backgroundColor": _navy,
                            "textFormat": {"foregroundColor": _white, "bold": True, "fontSize": 10},
                        }},
                        "fields": "userEnteredFormat(backgroundColor,textFormat)",
                    }
                }
                for r in _header_ranges
            ]
            # Auto-resize all columns including budget table
            _fmt_requests.append({
                "autoResizeDimensions": {"dimensions": {
                    "sheetId": _dash_id, "dimension": "COLUMNS",
                    "startIndex": 0, "endIndex": 17,  # Up to column Q
                }}
            })
            # Budget % Used column (O = index 14): green/yellow/red conditional formatting
            # Green: < 80%
            _fmt_requests.append({"addConditionalFormatRule": {"rule": {
                "ranges": [{"sheetId": _dash_id, "startRowIndex": 1, "startColumnIndex": 14, "endColumnIndex": 15}],
                "booleanRule": {
                    "condition": {"type": "NUMBER_LESS", "values": [{"userEnteredValue": "80"}]},
                    "format": {"backgroundColor": {"red": 0.85, "green": 0.96, "blue": 0.85}},
                },
            }, "index": 0}})
            # Yellow: 80–99%
            _fmt_requests.append({"addConditionalFormatRule": {"rule": {
                "ranges": [{"sheetId": _dash_id, "startRowIndex": 1, "startColumnIndex": 14, "endColumnIndex": 15}],
                "booleanRule": {
                    "condition": {"type": "NUMBER_BETWEEN", "values": [{"userEnteredValue": "80"}, {"userEnteredValue": "100"}]},
                    "format": {"backgroundColor": {"red": 1.0, "green": 0.96, "blue": 0.80}},
                },
            }, "index": 0}})
            # Red: >= 100%
            _fmt_requests.append({"addConditionalFormatRule": {"rule": {
                "ranges": [{"sheetId": _dash_id, "startRowIndex": 1, "startColumnIndex": 14, "endColumnIndex": 15}],
                "booleanRule": {
                    "condition": {"type": "NUMBER_GREATER_THAN_EQ", "values": [{"userEnteredValue": "100"}]},
                    "format": {"backgroundColor": {"red": 1.0, "green": 0.87, "blue": 0.87}},
                },
            }, "index": 0}})
            _svc.spreadsheets().batchUpdate(
                spreadsheetId=sheets_client._spreadsheet_id,
                body={"requests": _fmt_requests},
            ).execute()
    except Exception as e:
        logger.warning("Dashboard formatting failed: %s", e)

    # ── 5. Create/refresh charts via Sheets API ───────────────────
    try:
        from googleapiclient.discovery import build  # type: ignore[import-untyped]

        service = build("sheets", "v4", credentials=sheets_client._creds)
        spreadsheet_id = sheets_client._spreadsheet_id

        meta = service.spreadsheets().get(spreadsheetId=spreadsheet_id).execute()
        sheet_id_map = {s["properties"]["title"]: s["properties"]["sheetId"] for s in meta["sheets"]}
        dashboard_id = sheet_id_map.get("Dashboard")
        if dashboard_id is None:
            logger.warning("Dashboard sheet ID not found, skipping charts")
            return

        # Delete existing charts
        existing_charts = [
            c for s in meta["sheets"]
            if s["properties"]["sheetId"] == dashboard_id
            for c in s.get("charts", [])
        ]
        delete_requests = [
            {"deleteEmbeddedObject": {"objectId": c["chartId"]}}
            for c in existing_charts
        ]

        n_cats = len(cat_rows)
        n_months = len(month_rows)

        add_requests = []
        if n_cats > 0:
            add_requests.append(_donut_chart(dashboard_id, n_cats))
        if n_months > 0:
            add_requests.append(_monthly_expenses_chart(dashboard_id, n_months, currency))
        if n_months > 0:
            add_requests.append(_income_vs_expenses_chart(dashboard_id, n_months, currency))

        if delete_requests or add_requests:
            service.spreadsheets().batchUpdate(
                spreadsheetId=spreadsheet_id,
                body={"requests": delete_requests + add_requests},
            ).execute()
            logger.info("Dashboard charts created/updated")

    except Exception as e:
        logger.warning("Could not create charts: %s", e)

    # ── 6. Sync category conditional colors ───────────────────────────
    sheets_client.sync_category_colors()

    # ── 7. Refresh YoY Trends tab ─────────────────────────────────────
    try:
        from spectra.trends import refresh_trends
        refresh_trends(sheets_client, currency)
    except Exception as e:
        logger.warning("Trends tab update failed: %s", e)


# ── Chart builders ────────────────────────────────────────────────


def _donut_chart(sheet_id: int, n_rows: int) -> dict[str, Any]:
    return {
        "addChart": {
            "chart": {
                "spec": {
                    "title": "Spending by Category",
                    "pieChart": {
                        "legendPosition": "RIGHT_LEGEND",
                        "pieHole": 0.4,
                        "domain": {"sourceRange": {"sources": [_range(sheet_id, 1, 0, 1 + n_rows, 1)]}},
                        "series": {"sourceRange": {"sources": [_range(sheet_id, 1, 1, 1 + n_rows, 2)]}},
                    },
                },
                "position": _anchor(sheet_id, row=1, col=12),
            }
        }
    }


def _monthly_expenses_chart(sheet_id: int, n_rows: int, currency: str = "EUR") -> dict[str, Any]:
    return {
        "addChart": {
            "chart": {
                "spec": {
                    "title": "Monthly Expenses",
                    "basicChart": {
                        "chartType": "COLUMN",
                        "legendPosition": "BOTTOM_LEGEND",
                        "axis": [
                            {"position": "BOTTOM_AXIS", "title": "Month"},
                            {"position": "LEFT_AXIS", "title": currency},
                        ],
                        "domains": [
                            {"domain": {"sourceRange": {"sources": [_range(sheet_id, 1, 3, 1 + n_rows, 4)]}}}
                        ],
                        "series": [
                            {"series": {"sourceRange": {"sources": [_range(sheet_id, 1, 5, 1 + n_rows, 6)]}},
                             "targetAxis": "LEFT_AXIS"}
                        ],
                    },
                },
                "position": _anchor(sheet_id, row=20, col=12),
            }
        }
    }


def _income_vs_expenses_chart(sheet_id: int, n_rows: int, currency: str = "EUR") -> dict[str, Any]:
    return {
        "addChart": {
            "chart": {
                "spec": {
                    "title": "Income vs Expenses",
                    "basicChart": {
                        "chartType": "COLUMN",
                        "stackedType": "STACKED",
                        "legendPosition": "BOTTOM_LEGEND",
                        "axis": [
                            {"position": "BOTTOM_AXIS", "title": "Month"},
                            {"position": "LEFT_AXIS", "title": currency},
                        ],
                        "domains": [
                            {"domain": {"sourceRange": {"sources": [_range(sheet_id, 1, 3, 1 + n_rows, 4)]}}}
                        ],
                        "series": [
                            {"series": {"sourceRange": {"sources": [_range(sheet_id, 1, 4, 1 + n_rows, 5)]}},
                             "targetAxis": "LEFT_AXIS"},
                            {"series": {"sourceRange": {"sources": [_range(sheet_id, 1, 5, 1 + n_rows, 6)]}},
                             "targetAxis": "LEFT_AXIS"},
                        ],
                    },
                },
                "position": _anchor(sheet_id, row=40, col=12),
            }
        }
    }


def _range(sheet_id: int, r1: int, c1: int, r2: int, c2: int) -> dict[str, Any]:
    return {
        "sheetId": sheet_id,
        "startRowIndex": r1, "endRowIndex": r2,
        "startColumnIndex": c1, "endColumnIndex": c2,
    }


def _anchor(sheet_id: int, row: int, col: int) -> dict[str, Any]:
    return {
        "overlayPosition": {
            "anchorCell": {"sheetId": sheet_id, "rowIndex": row, "columnIndex": col},
            "widthPixels": 600,
            "heightPixels": 350,
        }
    }
