from __future__ import annotations

from decimal import Decimal

from spectra.budget import compute_budget_status, read_budgets
from spectra.dashboard import _monthly_expenses_chart, _summarize_rows
from spectra.trends import compute_monthly_data


class _BudgetWorksheet:
    def __init__(self, rows: list[list[str]]) -> None:
        self.rows = rows

    def get_all_values(self) -> list[list[str]]:
        return self.rows


class _Spreadsheet:
    def __init__(self, rows: list[list[str]]) -> None:
        self.budget = _BudgetWorksheet(rows)

    def worksheet(self, name: str) -> _BudgetWorksheet:
        if name != "Budget":
            raise KeyError(name)
        return self.budget


class _SheetsClient:
    def __init__(self, rows: list[list[str]]) -> None:
        self._spreadsheet = _Spreadsheet(rows)


def test_sheets_dashboard_aggregates_only_the_selected_denomination() -> None:
    rows = [
        ["Date", "Original Description", "Merchant", "Category", "Amount", "Currency", "Recurring"],
        ["2026-01-02", "rent", "Rent", "Housing", "-10.005", "EUR", "Subscription"],
        ["2026-01-03", "rent", "Rent", "Housing", "-20.000", "KWD", "Subscription"],
        ["2026-01-04", "coffee", "Cafe", "Food", "-4.00", "USD", ""],
        ["2026-01-05", "unknown", "Unknown", "Other", "-999.00", "", "Subscription"],
    ]

    categories, monthly, recurring, count = _summarize_rows(rows, "KWD")

    assert categories == {"Housing": Decimal("20.000")}
    assert monthly == {
        "2026-01": {"income": Decimal(0), "expenses": Decimal("20.000")}
    }
    assert recurring["rent"]["total"] == Decimal("-20.000")
    assert count == 1


def test_sheet_trends_filter_currencies_and_preserve_three_digit_precision() -> None:
    rows = [
        ["Date", "Amount", "Currency"],
        ["2026-01-01", "3.000", "KWD"],
        ["2026-01-02", "-1.234", "KWD"],
        ["2026-01-03", "-900.00", "EUR"],
        ["2026-01-04", "-700.00", ""],
    ]

    data = compute_monthly_data(rows, "KWD")

    assert data[2026][1] == {
        "income": 3.0,
        "expenses": 1.234,
        "net": 1.766,
        "savings_rate": 58.9,
    }


def test_legacy_euro_sheet_budget_is_not_relabelled_as_another_currency() -> None:
    sheets = _SheetsClient([
        ["Category", "Monthly Budget (€)"],
        ["Housing", "100.00"],
    ])

    assert read_budgets(sheets, "EUR") == {"Housing": Decimal("100.00")}
    assert read_budgets(sheets, "USD") == {}


def test_sheet_budget_status_uses_currency_precision_for_exact_internal_math() -> None:
    status = compute_budget_status(
        {"Housing": Decimal("1.234")},
        {"Housing": Decimal("2.000")},
        "KWD",
    )

    assert status == [{
        "category": "Housing",
        "spent": 1.234,
        "budget": 2.0,
        "pct": 61.7,
        "status": "🟢 OK",
    }]


def test_dashboard_chart_axis_uses_selected_currency_code() -> None:
    chart = _monthly_expenses_chart(7, 2, "KWD")

    assert chart["addChart"]["chart"]["spec"]["basicChart"]["axis"][1]["title"] == "KWD"

