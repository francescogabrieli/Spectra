"""Spectra Web Dashboard — FastAPI backend."""

from __future__ import annotations

import logging
import csv
import io
import re
import shutil
import tempfile
from datetime import date
from pathlib import Path
from typing import Any

from fastapi import FastAPI, File, Query, Request, UploadFile
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from spectra.ai import CategorisedTransaction
from spectra.config import Settings, load_settings
from spectra.cycles import (
    CYCLE_MODE_FIXED,
    DEFAULT_CYCLE_RULE,
    DEFAULT_CYCLE_START_DAY,
    MAX_CYCLE_START_DAY,
    VALID_CYCLE_MODES,
    cycle_start_for,
    cycle_window_for,
    format_cycle_label,
    next_cycle_start,
    parse_cycle_rule,
    normalize_cycle_start_day,
    parse_iso_date,
    serialize_cycle_rule,
)
from spectra.db import BookmarkDB
from spectra.ml_classifier import build_seed_data
from spectra.pipeline import _enrich_categorised_transactions, _load_existing_transfer_candidates
from spectra.recurring import detect_recurring_kind
from spectra.review import (
    TRANSFER_STATUS_CONFIRMED,
    TRANSFER_STATUS_DISMISSED,
    TRANSFER_STATUS_NONE,
    TRANSFER_STATUS_SUGGESTED,
    apply_review_state,
    build_import_batch_id,
    match_internal_transfers,
)
from spectra.rules import VALID_RULE_TYPES, normalize_rule_type

logger = logging.getLogger("spectra.web")

_HERE = Path(__file__).parent
_TEMPLATES = _HERE / "templates"
_STATIC = _HERE / "static"

app = FastAPI(title="Spectra Dashboard", docs_url=None, redoc_url=None)
app.mount("/static", StaticFiles(directory=str(_STATIC)), name="static")
templates = Jinja2Templates(directory=str(_TEMPLATES))

_THEME_SETTING_KEY = "theme_preference"
_CYCLE_RULE_SETTING_KEY = "cycle_start_day"
_BASE_CURRENCY_SETTING_KEY = "base_currency"
_VALID_THEME_PREFERENCES = {"auto", "light", "dark"}
_VALID_SUMMARY_SCOPES = {"cycle", "90d", "ytd"}
_CURRENCY_CODE_RE = re.compile(r"^[A-Z]{3}$")
_SETUP_SETTINGS_URL = "/settings?setup=currency"
_BASE_CURRENCY_SETUP_MESSAGE = (
    "Base currency not set. Open Settings and save your base currency before importing."
)
_TRANSACTION_EXPORT_HEADERS = [
    "tx_id",
    "date",
    "merchant",
    "category",
    "amount",
    "currency",
    "original_amount",
    "original_currency",
    "original_description",
    "counterpart",
    "recurring",
    "classification_source",
    "category_confidence",
    "needs_review",
    "review_reason",
    "account_name",
    "import_batch_id",
    "transfer_group_id",
    "transfer_status",
    "excluded_from_spend",
]


# ── Global error handler ─────────────────────────────────────────


from fastapi.responses import JSONResponse as _JSONResponse
from starlette.exceptions import HTTPException as StarletteHTTPException


@app.exception_handler(StarletteHTTPException)
async def http_exception_handler(request, exc):
    return _JSONResponse({"error": str(exc.detail)}, status_code=exc.status_code)


@app.exception_handler(Exception)
async def generic_exception_handler(request, exc):
    logger.exception("Unhandled error: %s", exc)
    return _JSONResponse({"error": "Internal server error"}, status_code=500)

def _get_db() -> BookmarkDB:
    settings = load_settings()
    return BookmarkDB(settings.db_path)


def _load_app_preferences(db: BookmarkDB | None = None) -> dict[str, Any]:
    owns_db = db is None
    db = db or _get_db()
    try:
        raw_theme = (db.get_app_setting(_THEME_SETTING_KEY, "auto") or "auto").strip().lower()
        theme_preference = raw_theme if raw_theme in _VALID_THEME_PREFERENCES else "auto"

        raw_cycle_rule = db.get_app_setting(_CYCLE_RULE_SETTING_KEY, DEFAULT_CYCLE_RULE)
        try:
            cycle_mode, fixed_cycle_start_day = parse_cycle_rule(
                raw_cycle_rule,
                clamp_legacy_day=True,
            )
            cycle_rule = serialize_cycle_rule(cycle_mode, fixed_cycle_start_day)
        except ValueError:
            cycle_mode = CYCLE_MODE_FIXED
            fixed_cycle_start_day = DEFAULT_CYCLE_START_DAY
            cycle_rule = DEFAULT_CYCLE_RULE
    finally:
        if owns_db:
            db.close()

    return {
        "theme_preference": theme_preference,
        "cycle_mode": cycle_mode,
        "cycle_rule": cycle_rule,
        "fixed_cycle_start_day": fixed_cycle_start_day,
        "pay_day": fixed_cycle_start_day,
        # Backward compatibility for older clients.
        "cycle_start_day": fixed_cycle_start_day,
    }


def _build_cycle_payload(cycle_rule: str):
    from datetime import date

    cycle_start, cycle_end = cycle_window_for(date.today(), cycle_rule)
    cycle_mode, fixed_cycle_start_day = parse_cycle_rule(cycle_rule, clamp_legacy_day=True)
    return {
        "start": cycle_start.isoformat(),
        "end": cycle_end.isoformat(),
        "label": format_cycle_label(cycle_start, cycle_end),
        "cycle_mode": cycle_mode,
        "cycle_rule": cycle_rule,
        "fixed_cycle_start_day": fixed_cycle_start_day,
        "pay_day": cycle_start.day,
        # Backward compatibility.
        "start_day": cycle_start.day,
    }


def _build_cycle_burn_rate(*, today, period_start, period_end, total_spent: float) -> dict[str, Any]:
    """Estimate end-of-cycle spend based on the current daily burn."""
    total_days = max((period_end - period_start).days, 1)
    elapsed_days = min(max((today - period_start).days + 1, 1), total_days)
    remaining_days = max(total_days - elapsed_days, 0)
    daily_spend = total_spent / elapsed_days
    projected_total = daily_spend * total_days

    return {
        "elapsed_days": elapsed_days,
        "total_days": total_days,
        "remaining_days": remaining_days,
        "spent_so_far": round(total_spent, 2),
        "daily_spend": round(daily_spend, 2),
        "projected_total": round(projected_total, 2),
    }


def _normalize_currency_code(value: str | None) -> str | None:
    code = str(value or "").strip().upper()
    if not code:
        return None
    if not _CURRENCY_CODE_RE.fullmatch(code):
        return None
    return code


def _resolve_base_currency(settings: Settings, db: BookmarkDB) -> str:
    stored = _normalize_currency_code(db.get_app_setting(_BASE_CURRENCY_SETTING_KEY))
    if stored:
        return stored
    from_env = _normalize_currency_code(settings.base_currency)
    return from_env or "EUR"


def _provider_is_ready(settings: Settings) -> bool:
    if settings.ai_provider == "local":
        return True
    if settings.ai_provider == "openai":
        return bool(settings.openai_api_key)
    return bool(settings.gemini_api_key)


def _build_setup_status(settings: Settings, db: BookmarkDB) -> dict[str, Any]:
    stored_base_currency = _normalize_currency_code(db.get_app_setting(_BASE_CURRENCY_SETTING_KEY))
    effective_currency = _resolve_base_currency(settings, db)
    tx_count_row = db._conn.execute("SELECT COUNT(*) FROM tx_history").fetchone()
    tx_count = int(tx_count_row[0] if tx_count_row else 0)
    has_existing_transactions = tx_count > 0
    requires_base_currency_setup = stored_base_currency is None and not has_existing_transactions
    google_sheets_configured = bool(settings.spreadsheet_id)
    provider_ready = _provider_is_ready(settings)
    can_start_import = not requires_base_currency_setup and provider_ready

    steps = [
        {
            "id": "base_currency",
            "label": "Choose base currency",
            "status": "required" if requires_base_currency_setup else "complete",
            "required": True,
            "detail": (
                "Required before the first import."
                if requires_base_currency_setup
                else f"Saved as {effective_currency}."
            ),
            "action_label": "Save base currency",
            "action_href": "#settings-currency",
        },
        {
            "id": "provider",
            "label": "Confirm categorization mode",
            "status": "complete" if provider_ready else "attention",
            "required": True,
            "detail": (
                f"{settings.ai_provider} is active."
                if provider_ready
                else f"{settings.ai_provider} is selected but still missing credentials."
            ),
            "action_label": "Review provider",
            "action_href": "#settings-provider",
        },
        {
            "id": "google_sheets",
            "label": "Optional Google Sheets sync",
            "status": "complete" if google_sheets_configured else "optional",
            "required": False,
            "detail": (
                "Connected."
                if google_sheets_configured
                else "Optional. Spectra works fully locally without it."
            ),
            "action_label": "Review sync",
            "action_href": "#settings-provider",
        },
        {
            "id": "first_import",
            "label": "Start first import",
            "status": "ready" if can_start_import else "blocked",
            "required": True,
            "detail": (
                "Upload a CSV, PDF, or OFX export."
                if can_start_import
                else "Complete the required steps above first."
            ),
            "action_label": "Continue to upload",
            "action_href": "/upload",
        },
    ]

    if requires_base_currency_setup:
        recommended_next_action = "Save base currency"
    elif not provider_ready:
        recommended_next_action = "Review provider credentials"
    elif not has_existing_transactions:
        recommended_next_action = "Continue to upload"
    else:
        recommended_next_action = "Review setup"

    return {
        "is_first_run": not has_existing_transactions,
        "requires_base_currency_setup": requires_base_currency_setup,
        "base_currency_configured": stored_base_currency is not None,
        "base_currency_source": "saved" if stored_base_currency else "default",
        "provider": settings.ai_provider,
        "provider_ready": provider_ready,
        "google_sheets_configured": google_sheets_configured,
        "has_existing_transactions": has_existing_transactions,
        "can_start_import": can_start_import,
        "recommended_next_action": recommended_next_action,
        "steps": steps,
        "settings_url": _SETUP_SETTINGS_URL,
    }


def _requires_base_currency_setup(db: BookmarkDB) -> bool:
    settings = load_settings()
    return bool(_build_setup_status(settings, db)["requires_base_currency_setup"])


def _base_currency_setup_response(*, include_ok: bool) -> JSONResponse:
    payload: dict[str, Any] = {
        "error": _BASE_CURRENCY_SETUP_MESSAGE,
        "message": _BASE_CURRENCY_SETUP_MESSAGE,
        "requires_base_currency_setup": True,
        "settings_url": _SETUP_SETTINGS_URL,
    }
    if include_ok:
        payload["ok"] = False
    return JSONResponse(payload, status_code=400)


def _setup_redirect_if_needed(request: Request) -> RedirectResponse | None:
    if request.url.path == "/settings":
        return None
    settings = load_settings()
    with BookmarkDB(settings.db_path) as db:
        if _build_setup_status(settings, db)["requires_base_currency_setup"]:
            return RedirectResponse(url=_SETUP_SETTINGS_URL, status_code=303)
    return None


def _template_context(request: Request) -> dict[str, Any]:
    return {"request": request, **_load_app_preferences()}


def _coerce_bool(value: Any, default: bool = False) -> bool:
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return bool(value)
    if isinstance(value, str):
        normalized = value.strip().lower()
        if normalized in {"1", "true", "yes", "on"}:
            return True
        if normalized in {"0", "false", "no", "off"}:
            return False
    return default


def _csv_safe_value(value: Any) -> Any:
    if value is None:
        return ""
    if isinstance(value, bool):
        return "1" if value else "0"
    return value


def _transactions_to_csv(rows: list[dict[str, Any]]) -> str:
    buffer = io.StringIO()
    writer = csv.writer(buffer)
    writer.writerow(_TRANSACTION_EXPORT_HEADERS)
    for row in rows:
        writer.writerow([
            _csv_safe_value(row.get("id")),
            _csv_safe_value(row.get("date")),
            _csv_safe_value(row.get("merchant")),
            _csv_safe_value(row.get("category")),
            _csv_safe_value(row.get("amount")),
            _csv_safe_value(row.get("currency")),
            _csv_safe_value(row.get("original_amount")),
            _csv_safe_value(row.get("original_currency")),
            _csv_safe_value(row.get("original_description")),
            _csv_safe_value(row.get("counterpart")),
            _csv_safe_value(row.get("recurring")),
            _csv_safe_value(row.get("classification_source")),
            _csv_safe_value(row.get("category_confidence")),
            _csv_safe_value(int(bool(row.get("needs_review")))),
            _csv_safe_value(row.get("review_reason")),
            _csv_safe_value(row.get("account_name")),
            _csv_safe_value(row.get("import_batch_id")),
            _csv_safe_value(row.get("transfer_group_id")),
            _csv_safe_value(row.get("transfer_status")),
            _csv_safe_value(int(bool(row.get("excluded_from_spend")))),
        ])
    return buffer.getvalue()


def _persist_learning(
    db: BookmarkDB,
    *,
    tx_id: str | None,
    original_description: str,
    clean_name: str,
    category: str,
    source: str,
    apply_to_future: bool,
) -> None:
    normalized_name = str(clean_name or "").strip()
    normalized_category = str(category or "").strip()
    normalized_original = str(original_description or "")
    if not normalized_name or not normalized_category or normalized_category == "Uncategorized":
        return

    if apply_to_future:
        db.save_merchant_category(normalized_name, normalized_category)
        if normalized_original:
            db.save_overrides(
                {
                    normalized_original: {
                        "clean_name": normalized_name,
                        "category": normalized_category,
                    }
                }
            )

    db.record_learning_feedback(
        tx_id=tx_id,
        original_description=normalized_original,
        clean_name=normalized_name,
        category=normalized_category,
        source=source,
        apply_to_future=apply_to_future,
    )


def _normalize_transfer_category(amount: float) -> str:
    return "Transfer In" if float(amount) > 0 else "Transfer"


def _refresh_review_state(db: BookmarkDB, tx_id: str) -> dict[str, Any] | None:
    tx = db.get_transaction(tx_id)
    if not tx:
        return None
    needs_review, review_reason = apply_review_state(tx)
    db.update_transaction(
        tx_id,
        needs_review=1 if needs_review else 0,
        review_reason=review_reason,
    )
    return db.get_transaction(tx_id)


def _serialize_transaction_model(tx: CategorisedTransaction, *, include_preview_only: bool = False) -> dict[str, Any]:
    row = {
        "id": tx.id,
        "date": tx.date,
        "merchant": tx.clean_name,
        "category": tx.category,
        "amount": tx.amount,
        "currency": tx.currency,
        "recurring": tx.recurring,
        "original_description": tx.original_description,
        "counterpart": tx.counterpart,
        "classification_source": tx.classification_source,
        "category_confidence": tx.category_confidence,
        "needs_review": tx.needs_review,
        "review_reason": tx.review_reason,
        "account_name": tx.account_name,
        "import_batch_id": tx.import_batch_id,
        "transfer_group_id": tx.transfer_group_id,
        "transfer_status": tx.transfer_status,
        "excluded_from_spend": tx.excluded_from_spend,
        "original_amount": tx.original_amount,
        "original_currency": tx.original_currency,
    }
    if getattr(tx, "category_suggestions", None):
        row["category_suggestions"] = [
            suggestion.model_dump()
            if hasattr(suggestion, "model_dump")
            else dict(suggestion)
            for suggestion in tx.category_suggestions
        ]
    if include_preview_only and not row["classification_source"]:
        row.pop("classification_source", None)
    return row


def _payload_to_transaction(payload: dict[str, Any], *, base_currency: str) -> CategorisedTransaction:
    return CategorisedTransaction(
        id=str(payload["id"]),
        original_description=str(payload.get("original_description", "") or ""),
        clean_name=str(payload.get("merchant", "") or ""),
        category=str(payload.get("category", "Uncategorized") or "Uncategorized"),
        amount=float(payload.get("amount", 0) or 0),
        currency=str(payload.get("currency", base_currency) or base_currency),
        original_amount=payload.get("original_amount"),
        original_currency=payload.get("original_currency"),
        date=str(payload.get("date", "") or ""),
        statement_category=str(payload.get("statement_category", "") or ""),
        recurring=str(payload.get("recurring", "") or ""),
        counterpart=str(payload.get("counterpart", "") or ""),
        classification_source=str(payload.get("classification_source", "") or ""),
        category_confidence=payload.get("category_confidence"),
        needs_review=bool(payload.get("needs_review", False)),
        review_reason=str(payload.get("review_reason", "") or ""),
        account_name=str(payload.get("account_name", "") or ""),
        import_batch_id=str(payload.get("import_batch_id", "") or ""),
        transfer_group_id=str(payload.get("transfer_group_id", "") or ""),
        transfer_status=str(payload.get("transfer_status", TRANSFER_STATUS_NONE) or TRANSFER_STATUS_NONE),
        excluded_from_spend=bool(payload.get("excluded_from_spend", False)),
    )


def _simulate_rule_impact(
    db: BookmarkDB,
    *,
    rule_type: str,
    pattern: str,
    sample_text: str = "",
) -> dict[str, Any]:
    from spectra.rules import match_rule

    candidate_rule = {
        "rule_type": rule_type,
        "pattern": pattern,
        "is_active": True,
    }
    rows = db._conn.execute(
        """
        SELECT tx_id, date, clean_name, original_description, category
        FROM tx_history
        ORDER BY date DESC, tx_id DESC
        """
    ).fetchall()

    examples: list[dict[str, Any]] = []
    impact_count = 0
    for tx_id, date_str, clean_name, original_description, category in rows:
        if not match_rule(
            candidate_rule,
            clean_name=str(clean_name),
            raw_description=str(original_description or clean_name),
        ):
            continue

        impact_count += 1
        if len(examples) < 5:
            examples.append(
                {
                    "tx_id": str(tx_id),
                    "date": str(date_str),
                    "merchant": str(clean_name),
                    "original_description": str(original_description or ""),
                    "current_category": str(category),
                }
            )

    matches_sample = False
    if sample_text:
        matches_sample = match_rule(
            candidate_rule,
            clean_name=sample_text,
            raw_description=sample_text,
        )

    return {
        "matches_sample": matches_sample,
        "impact_count": impact_count,
        "examples": examples,
    }


def _build_summary_insights(
    *,
    scope: str,
    rows: list[tuple[str, str, float, str]],
    period_start,
    period_end,
    cycle_rule: str,
    total_spent: float,
    uncategorized: int,
    burn_rate: dict[str, Any] | None,
    budget_limits: dict[str, float],
) -> list[dict[str, str]]:
    from collections import defaultdict
    from datetime import timedelta
    from statistics import median

    def fmt(amount: float) -> str:
        return f"EUR {amount:,.2f}".replace(",", "_").replace(".", ",").replace("_", ".")

    insights: list[dict[str, str]] = []

    if scope == "cycle" and burn_rate:
        total_budget = round(sum(limit for limit in budget_limits.values() if limit and limit > 0), 2)
        if total_budget > 0:
            projected_total = float(burn_rate.get("projected_total", 0.0))
            if projected_total > total_budget:
                insights.append(
                    {
                        "type": "budget_risk",
                        "severity": "warning",
                        "title": "Projected cycle spend is above your current budget",
                        "detail": f"Projected {fmt(projected_total)} vs configured budget {fmt(total_budget)}.",
                        "href": "/budget",
                    }
                )
            elif projected_total > total_budget * 0.9:
                insights.append(
                    {
                        "type": "budget_watch",
                        "severity": "info",
                        "title": "Cycle spend is close to the configured budget",
                        "detail": f"Projected {fmt(projected_total)} against budget {fmt(total_budget)}.",
                        "href": "/budget",
                    }
                )

    if uncategorized > 0:
        insights.append(
            {
                "type": "uncategorized",
                "severity": "warning",
                "title": "Some transactions still need a category",
                "detail": f"{uncategorized} transaction(s) are still uncategorized in the selected period.",
                "href": "/transactions",
            }
        )

    if scope != "cycle":
        return insights[:4]

    current_by_category: dict[str, float] = defaultdict(float)
    previous_by_category: dict[str, float] = defaultdict(float)
    history_by_merchant: dict[str, list[float]] = defaultdict(list)
    current_expenses: list[tuple[str, float, str]] = []
    subscription_by_merchant: dict[str, list[tuple[object, float]]] = defaultdict(list)

    previous_cycle_start = cycle_start_for(period_start - timedelta(days=1), cycle_rule)
    previous_cycle_end = period_start

    for tx_date_str, clean_name, amount, category in rows:
        tx_date = parse_iso_date(tx_date_str)
        if amount >= 0:
            continue

        spend = abs(float(amount))
        merchant = str(clean_name)
        category_name = str(category)

        if tx_date < period_start:
            history_by_merchant[merchant].append(spend)
        if previous_cycle_start <= tx_date < previous_cycle_end:
            previous_by_category[category_name] += spend
        if period_start <= tx_date < period_end:
            current_expenses.append((merchant, spend, category_name))
            current_by_category[category_name] += spend

        if category_name == "Digital Subscriptions":
            subscription_by_merchant[merchant].append((tx_date, spend))

    biggest_delta = None
    for category_name, current_total in current_by_category.items():
        previous_total = previous_by_category.get(category_name, 0.0)
        delta = current_total - previous_total
        if delta <= max(25.0, previous_total * 0.25):
            continue
        if biggest_delta is None or delta > biggest_delta[1]:
            biggest_delta = (category_name, delta, previous_total, current_total)

    if biggest_delta:
        category_name, delta, previous_total, current_total = biggest_delta
        previous_text = fmt(previous_total) if previous_total else "EUR 0,00"
        insights.append(
            {
                "type": "category_delta",
                "severity": "info",
                "title": f"{category_name} is the main driver this cycle",
                "detail": f"Up by {fmt(delta)} versus the previous cycle ({fmt(current_total)} vs {previous_text}).",
                "href": "/trends",
            }
        )

    anomalies: list[tuple[str, float, float]] = []
    first_time_large: list[tuple[str, float]] = []
    for merchant, spend, _category_name in current_expenses:
        past_amounts = history_by_merchant.get(merchant, [])
        if len(past_amounts) >= 2:
            baseline = float(median(past_amounts))
            if baseline > 0 and spend > baseline * 1.8 and (spend - baseline) >= 20:
                anomalies.append((merchant, spend, baseline))
        elif not past_amounts and spend >= 150:
            first_time_large.append((merchant, spend))

    if anomalies:
        anomalies.sort(key=lambda item: item[1] - item[2], reverse=True)
        merchant, spend, baseline = anomalies[0]
        insights.append(
            {
                "type": "anomaly",
                "severity": "warning",
                "title": f"{len(anomalies)} unusual charge(s) detected",
                "detail": f"{merchant} posted {fmt(spend)} vs a usual baseline near {fmt(baseline)}.",
                "href": "/transactions",
            }
        )
    elif first_time_large:
        first_time_large.sort(key=lambda item: item[1], reverse=True)
        merchant, spend = first_time_large[0]
        insights.append(
            {
                "type": "first_time_large",
                "severity": "info",
                "title": "Large first-time expense found in this cycle",
                "detail": f"{merchant} appears as a new merchant with a {fmt(spend)} charge.",
                "href": "/transactions",
            }
        )

    subscription_changes: list[tuple[str, float, float]] = []
    for merchant, history in subscription_by_merchant.items():
        history.sort(key=lambda item: item[0])
        if len(history) < 2:
            continue
        latest_date, latest_amount = history[-1]
        if not (period_start <= latest_date < period_end):
            continue

        previous_amounts = [amount for _dt, amount in history[:-1]]
        baseline = float(median(previous_amounts)) if previous_amounts else 0.0
        diff = latest_amount - baseline
        if baseline > 0 and abs(diff) >= max(1.0, baseline * 0.08):
            subscription_changes.append((merchant, latest_amount, baseline))

    if subscription_changes:
        subscription_changes.sort(key=lambda item: abs(item[1] - item[2]), reverse=True)
        merchant, latest_amount, baseline = subscription_changes[0]
        direction = "up" if latest_amount > baseline else "down"
        insights.append(
            {
                "type": "subscription_change",
                "severity": "info",
                "title": "A recurring subscription changed price",
                "detail": f"{merchant} moved {direction} to {fmt(latest_amount)} from a prior baseline near {fmt(baseline)}.",
                "href": "/subscriptions",
            }
        )

    return insights[:4]


# ── Pages ────────────────────────────────────────────────────────


@app.get("/", response_class=HTMLResponse)
async def page_dashboard(request: Request):
    if (redirect := _setup_redirect_if_needed(request)):
        return redirect
    return templates.TemplateResponse(request, "dashboard.html", _template_context(request))


@app.get("/transactions", response_class=HTMLResponse)
async def page_transactions(request: Request):
    if (redirect := _setup_redirect_if_needed(request)):
        return redirect
    return templates.TemplateResponse(request, "transactions.html", _template_context(request))


@app.get("/upload", response_class=HTMLResponse)
async def page_upload(request: Request):
    if (redirect := _setup_redirect_if_needed(request)):
        return redirect
    return templates.TemplateResponse(request, "upload.html", _template_context(request))


@app.get("/settings", response_class=HTMLResponse)
async def page_settings(request: Request):
    return templates.TemplateResponse(request, "settings.html", _template_context(request))


@app.get("/review", response_class=HTMLResponse)
async def page_review(request: Request):
    if (redirect := _setup_redirect_if_needed(request)):
        return redirect
    return templates.TemplateResponse(request, "review.html", _template_context(request))


@app.get("/subscriptions", response_class=HTMLResponse)
async def page_subscriptions(request: Request):
    if (redirect := _setup_redirect_if_needed(request)):
        return redirect
    return templates.TemplateResponse(request, "subscriptions.html", _template_context(request))


# ── API: Dashboard Summary ───────────────────────────────────────


@app.get("/api/summary")
async def api_summary(scope: str = Query("cycle")):
    """Return dashboard-level stats."""
    scope = (scope or "cycle").strip().lower()
    if scope not in _VALID_SUMMARY_SCOPES:
        return JSONResponse(
            {"error": "scope must be one of cycle, 90d, ytd"},
            status_code=400,
        )

    preferences = _load_app_preferences()
    cycle_rule = preferences["cycle_rule"]

    from datetime import date, timedelta

    today = date.today()
    if scope == "cycle":
        period_start, period_end = cycle_window_for(today, cycle_rule)
        scope_label = format_cycle_label(period_start, period_end)
    elif scope == "90d":
        period_end = today + timedelta(days=1)
        period_start = period_end - timedelta(days=90)
        scope_label = f"Last 90 days ({format_cycle_label(period_start, period_end)})"
    else:  # ytd
        period_start = date(today.year, 1, 1)
        period_end = today + timedelta(days=1)
        scope_label = f"Year to date ({format_cycle_label(period_start, period_end)})"

    burn_rate = (
        _build_cycle_burn_rate(
            today=today,
            period_start=period_start,
            period_end=period_end,
            total_spent=0.0,
        )
        if scope == "cycle"
        else None
    )

    with _get_db() as db:
        rows = db._conn.execute(
            """
            SELECT date, clean_name, amount, category
            FROM tx_history
            WHERE excluded_from_spend = 0
            ORDER BY date DESC
            """
        ).fetchall()
        budget_limits = db.get_budget_limits()
        needs_review_count = db.count_review_queue()
        confirmed_internal_transfers_count = db.count_confirmed_transfer_groups()

    if not rows:
        return {
            "total_spent": 0, "total_income": 0, "subscriptions": 0,
            "uncategorized": 0, "uncategorized_total": 0, "by_category": {}, "monthly": {},
            "monthly_ranges": {}, "top_merchants": [], "current_cycle": _build_cycle_payload(cycle_rule),
            "scope": scope, "scope_label": scope_label,
            "selected_period": {"start": period_start.isoformat(), "end": period_end.isoformat(), "label": scope_label},
            **preferences,
            "has_data": False,
            "burn_rate": burn_rate,
            "insights": [],
            "needs_review_count": needs_review_count,
            "confirmed_internal_transfers_count": confirmed_internal_transfers_count,
        }

    from collections import Counter, defaultdict

    total_spent = 0.0
    total_income = 0.0
    subscriptions = 0.0
    uncategorized = 0
    uncategorized_total = sum(1 for _d, _m, _a, cat in rows if str(cat) == "Uncategorized")
    by_category: dict[str, float] = defaultdict(float)
    monthly: dict[str, float] = defaultdict(float)
    monthly_ranges: dict[str, dict[str, str]] = {}
    merchant_totals: Counter = Counter()
    in_scope_count = 0

    def _monthly_bucket(tx_date):
        if scope == "cycle":
            bucket_start = cycle_start_for(tx_date, cycle_rule)
            bucket_end = next_cycle_start(bucket_start, cycle_rule)
        else:
            bucket_start = tx_date.replace(day=1)
            if bucket_start.month == 12:
                bucket_end = bucket_start.replace(year=bucket_start.year + 1, month=1)
            else:
                bucket_end = bucket_start.replace(month=bucket_start.month + 1)
        return bucket_start, bucket_end

    for tx_date_str, clean_name, amount, cat in rows:
        tx_date = parse_iso_date(tx_date_str)
        if not (period_start <= tx_date < period_end):
            continue

        in_scope_count += 1

        bucket_start, bucket_end = _monthly_bucket(tx_date)
        month = bucket_start.isoformat()
        monthly_ranges[month] = {"start": bucket_start.isoformat(), "end": bucket_end.isoformat()}

        if amount < 0:
            monthly[month] += abs(amount)
            total_spent += abs(amount)
            by_category[cat] += abs(amount)
            merchant_totals[clean_name] += abs(amount)
        else:
            total_income += amount

        if cat == "Uncategorized":
            uncategorized += 1
        if cat == "Digital Subscriptions":
            subscriptions += abs(amount)

    # Last 6 months
    sorted_months = sorted(monthly.keys())[-6:]
    monthly_data = {m: round(monthly[m], 2) for m in sorted_months}
    monthly_ranges_data = {m: monthly_ranges[m] for m in sorted_months}

    # Top 5
    top5 = [{"name": n, "total": round(t, 2)} for n, t in merchant_totals.most_common(5)]

    if scope == "cycle":
        burn_rate = _build_cycle_burn_rate(
            today=today,
            period_start=period_start,
            period_end=period_end,
            total_spent=total_spent,
        )

    insights = _build_summary_insights(
        scope=scope,
        rows=rows,
        period_start=period_start,
        period_end=period_end,
        cycle_rule=cycle_rule,
        total_spent=total_spent,
        uncategorized=uncategorized,
        burn_rate=burn_rate,
        budget_limits=budget_limits,
    )

    return {
        "total_spent": round(total_spent, 2),
        "total_income": round(total_income, 2),
        "subscriptions": round(subscriptions, 2),
        "uncategorized": uncategorized,
        "uncategorized_total": uncategorized_total,
        "by_category": {k: round(v, 2) for k, v in sorted(by_category.items(), key=lambda x: -x[1])},
        "monthly": monthly_data,
        "monthly_ranges": monthly_ranges_data,
        "top_merchants": top5,
        "current_cycle": _build_cycle_payload(cycle_rule),
        "scope": scope,
        "scope_label": scope_label,
        "selected_period": {
            "start": period_start.isoformat(),
            "end": period_end.isoformat(),
            "label": scope_label,
        },
        **preferences,
        "has_data": in_scope_count > 0,
        "burn_rate": burn_rate,
        "insights": insights,
        "needs_review_count": needs_review_count,
        "confirmed_internal_transfers_count": confirmed_internal_transfers_count,
    }


# ── API: Transactions ────────────────────────────────────────────


@app.get("/api/transactions")
async def api_transactions(
    page: int = Query(1, ge=1),
    per_page: int = Query(50, ge=10, le=200),
    category: str = Query("", alias="category"),
    uncategorized_only: bool = Query(False),
    search: str = Query(""),
    date_from: str = Query(""),
    date_to: str = Query(""),
    needs_review: bool | None = Query(None),
    recurring: str = Query(""),
    account_name: str = Query(""),
    classification_source: str = Query(""),
    transfer_status: str = Query(""),
):
    """Return paginated transactions from history."""
    with _get_db() as db:
        return db.list_transactions(
            page=page,
            per_page=per_page,
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


@app.get("/api/exports/transactions")
async def api_export_transactions(
    category: str = Query("", alias="category"),
    uncategorized_only: bool = Query(False),
    search: str = Query(""),
    date_from: str = Query(""),
    date_to: str = Query(""),
    needs_review: bool | None = Query(None),
    recurring: str = Query(""),
    account_name: str = Query(""),
    classification_source: str = Query(""),
    transfer_status: str = Query(""),
):
    """Download the current transaction view as CSV."""
    with _get_db() as db:
        rows = db.list_all_transactions(
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

    csv_text = _transactions_to_csv(rows)
    filename = f"spectra_transactions_{date.today().isoformat()}.csv"
    return Response(
        content=csv_text,
        media_type="text/csv; charset=utf-8",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


@app.patch("/api/transactions/{tx_id}")
async def api_update_transaction(tx_id: str, request: Request):
    """Update merchant name and/or category for a transaction."""
    body = await request.json()
    new_category = body.get("category")
    new_merchant = body.get("merchant")
    apply_to_future = _coerce_bool(body.get("apply_to_future"), True)

    with _get_db() as db:
        tx = db.get_transaction(tx_id)
        if not tx:
            return JSONResponse({"error": "Transaction not found"}, status_code=404)

        original_description = str(tx["original_description"] or "")
        merchant_name = str(new_merchant or tx["merchant"])
        category_name = str(new_category or tx["category"])

        if new_merchant:
            db.update_transaction(tx_id, clean_name=merchant_name)

        if new_category:
            db.update_transaction(tx_id, category=new_category)

        _persist_learning(
            db,
            tx_id=tx_id,
            original_description=original_description,
            clean_name=str(merchant_name),
            category=str(category_name),
            source="manual_edit",
            apply_to_future=apply_to_future,
        )
        refreshed = _refresh_review_state(db, tx_id)

    return {"ok": True, "id": tx_id, "transaction": refreshed}


@app.post("/api/transactions/bulk-category")
async def api_bulk_update_category(request: Request):
    """Apply one category to multiple transactions quickly."""
    body = await request.json()
    ids = body.get("ids") or []
    category = str(body.get("category") or "").strip()
    apply_to_future = _coerce_bool(body.get("apply_to_future"), True)

    if not isinstance(ids, list) or not ids:
        return JSONResponse({"error": "ids must be a non-empty list"}, status_code=400)
    if not category:
        return JSONResponse({"error": "category is required"}, status_code=400)

    cleaned_ids = [str(v).strip() for v in ids if str(v).strip()]
    if not cleaned_ids:
        return JSONResponse({"error": "ids must contain valid transaction IDs"}, status_code=400)

    with _get_db() as db:
        placeholders = ",".join("?" for _ in cleaned_ids)
        merchant_rows = db._conn.execute(
            f"SELECT tx_id, clean_name, original_description FROM tx_history WHERE tx_id IN ({placeholders})",
            cleaned_ids,
        ).fetchall()
        db.update_transactions(cleaned_ids, category=category)

        for tx_row_id, merchant_name, original_description in merchant_rows:
            _persist_learning(
                db,
                tx_id=str(tx_row_id),
                original_description=str(original_description or ""),
                clean_name=str(merchant_name or ""),
                category=category,
                source="bulk_edit",
                apply_to_future=apply_to_future,
            )
            _refresh_review_state(db, str(tx_row_id))

    return {"ok": True, "updated": len(cleaned_ids), "category": category}


@app.get("/api/review")
async def api_review(
    page: int = Query(1, ge=1),
    per_page: int = Query(50, ge=10, le=200),
    reason: str = Query(""),
    transfer_status: str = Query(""),
    import_batch_id: str = Query(""),
):
    """Return review-queue items ordered by newest first."""
    with _get_db() as db:
        return db.get_review_queue(
            page=page,
            per_page=per_page,
            reason=reason,
            transfer_status=transfer_status,
            import_batch_id=import_batch_id,
        )


@app.get("/api/exports/review")
async def api_export_review(
    reason: str = Query(""),
    transfer_status: str = Query(""),
    import_batch_id: str = Query(""),
):
    """Download the current review queue as CSV."""
    with _get_db() as db:
        rows = db.list_all_review_queue(
            reason=reason,
            transfer_status=transfer_status,
            import_batch_id=import_batch_id,
        )

    csv_text = _transactions_to_csv(rows)
    filename = f"spectra_review_queue_{date.today().isoformat()}.csv"
    return Response(
        content=csv_text,
        media_type="text/csv; charset=utf-8",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


@app.patch("/api/review/{tx_id}")
async def api_review_transaction(tx_id: str, request: Request):
    """Resolve or update a review-queue item."""
    body = await request.json()
    new_category = body.get("category")
    new_merchant = body.get("merchant")
    apply_to_future = _coerce_bool(body.get("apply_to_future"), False)
    mark_reviewed = _coerce_bool(body.get("mark_reviewed"), False)

    with _get_db() as db:
        tx = db.get_transaction(tx_id)
        if not tx:
            return JSONResponse({"error": "Transaction not found"}, status_code=404)

        merchant_name = str(new_merchant or tx["merchant"])
        category_name = str(new_category or tx["category"])
        updates: dict[str, Any] = {}
        if new_merchant:
            updates["clean_name"] = merchant_name
        if new_category:
            updates["category"] = category_name
        if updates:
            db.update_transaction(tx_id, **updates)

        if new_merchant or new_category:
            _persist_learning(
                db,
                tx_id=tx_id,
                original_description=str(tx["original_description"] or ""),
                clean_name=merchant_name,
                category=category_name,
                source="review_edit",
                apply_to_future=apply_to_future,
            )

        refreshed = _refresh_review_state(db, tx_id)
        if mark_reviewed and refreshed and refreshed["transfer_status"] != TRANSFER_STATUS_SUGGESTED:
            db.update_transaction(tx_id, needs_review=0, review_reason="")
            refreshed = db.get_transaction(tx_id)

    return {"ok": True, "transaction": refreshed}


@app.post("/api/transfers/confirm")
async def api_confirm_transfer_group(request: Request):
    """Confirm or dismiss a suggested transfer pair."""
    body = await request.json()
    transfer_group_id = str(body.get("transfer_group_id") or "").strip()
    action = str(body.get("action") or "").strip().lower()

    if not transfer_group_id:
        return JSONResponse({"error": "transfer_group_id is required"}, status_code=400)
    if action not in {"confirm", "dismiss"}:
        return JSONResponse({"error": "action must be confirm or dismiss"}, status_code=400)

    with _get_db() as db:
        rows = db.get_transactions_by_transfer_group(transfer_group_id)
        if not rows:
            return JSONResponse({"error": "Transfer group not found"}, status_code=404)

        tx_ids = [str(row["id"]) for row in rows]
        if action == "confirm":
            for row in rows:
                db.update_transaction(
                    str(row["id"]),
                    category=_normalize_transfer_category(float(row["amount"])),
                    transfer_status=TRANSFER_STATUS_CONFIRMED,
                    excluded_from_spend=1,
                    needs_review=0,
                    review_reason="",
                )
        else:
            for row in rows:
                db.update_transaction(
                    str(row["id"]),
                    transfer_status=TRANSFER_STATUS_DISMISSED,
                    excluded_from_spend=0,
                )
                _refresh_review_state(db, str(row["id"]))

        updated_rows = [db.get_transaction(tx_id) for tx_id in tx_ids]

    return {"ok": True, "transfer_group_id": transfer_group_id, "action": action, "transactions": updated_rows}


# ── API: Categories ──────────────────────────────────────────────


@app.get("/api/categories")
async def api_categories():
    """Return all known categories."""
    with _get_db() as db:
        cats = db._conn.execute(
            "SELECT DISTINCT category FROM tx_history WHERE category != 'Uncategorized' ORDER BY category"
        ).fetchall()
    return {"categories": [row[0] for row in cats]}


@app.get("/api/categories/options")
async def api_categories_options():
    """Return all categories."""
    with _get_db() as db:
        cats = db._conn.execute(
            "SELECT DISTINCT category FROM tx_history WHERE category != 'Uncategorized' ORDER BY category"
        ).fetchall()
    known_cats = [row[0] for row in cats]
    other_cats = [row[1] for row in build_seed_data()]
    return {"categories": sorted(set(known_cats + other_cats))}

# ── API: Settings ────────────────────────────────────────────────


@app.get("/api/settings")
async def api_settings():
    """Return current config for the settings page."""
    settings = load_settings()
    with _get_db() as db:
        preferences = _load_app_preferences(db)
        effective_currency = _resolve_base_currency(settings, db)
        setup_status = _build_setup_status(settings, db)
        tx_count = db._conn.execute("SELECT COUNT(*) FROM tx_history").fetchone()[0]
        merchant_count = db._conn.execute("SELECT COUNT(*) FROM merchant_categories").fetchone()[0]
        feedback_count = db._conn.execute("SELECT COUNT(*) FROM learning_feedback").fetchone()[0]
        active_rule_count = db._conn.execute(
            "SELECT COUNT(*) FROM category_rules WHERE is_active = 1"
        ).fetchone()[0]
        cats = db._conn.execute(
            "SELECT DISTINCT category FROM tx_history WHERE category != 'Uncategorized'"
        ).fetchall()
    return {
        "currency": effective_currency,
        "tx_count": tx_count,
        "merchant_count": merchant_count,
        "feedback_count": feedback_count,
        "active_rule_count": active_rule_count,
        "category_count": len(cats),
        "sheets_connected": setup_status["google_sheets_configured"],
        **preferences,
        **setup_status,
        "setup_status": setup_status,
        "current_cycle": _build_cycle_payload(preferences["cycle_rule"]),
    }


@app.patch("/api/settings/preferences")
async def api_update_preferences(request: Request):
    """Save dashboard-local preferences such as theme and cycle start day."""
    body = await request.json()
    updates: dict[str, str] = {}

    if "theme_preference" in body:
        theme_preference = str(body.get("theme_preference", "")).strip().lower()
        if theme_preference not in _VALID_THEME_PREFERENCES:
            return JSONResponse(
                {"error": "theme_preference must be one of auto, light, dark"},
                status_code=400,
            )
        updates[_THEME_SETTING_KEY] = theme_preference

    if "base_currency" in body:
        base_currency = _normalize_currency_code(body.get("base_currency"))
        if not base_currency:
            return JSONResponse(
                {"error": "base_currency must be a valid 3-letter ISO code (e.g. EUR, USD, GBP)"},
                status_code=400,
            )
        updates[_BASE_CURRENCY_SETTING_KEY] = base_currency

    with _get_db() as db:
        raw_pay_day = body.get("pay_day", body.get("cycle_start_day"))
        raw_cycle_mode = body.get("cycle_mode")
        if raw_cycle_mode is not None or raw_pay_day is not None:
            current_preferences = _load_app_preferences(db)

            if raw_cycle_mode is None:
                cycle_mode = CYCLE_MODE_FIXED
            else:
                cycle_mode = str(raw_cycle_mode or "").strip().lower()
                if cycle_mode not in VALID_CYCLE_MODES:
                    return JSONResponse(
                        {"error": f"cycle_mode must be one of {', '.join(sorted(VALID_CYCLE_MODES))}"},
                        status_code=400,
                    )

            fixed_cycle_start_day = current_preferences["fixed_cycle_start_day"] or DEFAULT_CYCLE_START_DAY
            if raw_pay_day is not None:
                try:
                    fixed_cycle_start_day = normalize_cycle_start_day(int(raw_pay_day))
                except (TypeError, ValueError):
                    return JSONResponse(
                        {"error": f"pay_day must be an integer between 1 and {MAX_CYCLE_START_DAY}"},
                        status_code=400,
                    )
            elif cycle_mode == CYCLE_MODE_FIXED and current_preferences["cycle_mode"] != CYCLE_MODE_FIXED:
                fixed_cycle_start_day = DEFAULT_CYCLE_START_DAY

            updates[_CYCLE_RULE_SETTING_KEY] = serialize_cycle_rule(cycle_mode, fixed_cycle_start_day)

        if not updates:
            return JSONResponse({"error": "No supported preference fields provided"}, status_code=400)

        for key, value in updates.items():
            db.set_app_setting(key, value)
        preferences = _load_app_preferences(db)
        settings = load_settings()
        effective_currency = _resolve_base_currency(settings, db)
        setup_status = _build_setup_status(settings, db)
    return {
        "ok": True,
        **preferences,
        "currency": effective_currency,
        **setup_status,
        "setup_status": setup_status,
        "current_cycle": _build_cycle_payload(preferences["cycle_rule"]),
    }


@app.get("/api/settings/rules")
async def api_get_category_rules():
    """Return user-defined categorization rules."""
    with _get_db() as db:
        rules = db.get_category_rules()
    return {
        "rules": rules,
        "valid_rule_types": sorted(VALID_RULE_TYPES),
        "summary": {
            "total": len(rules),
            "active": sum(1 for rule in rules if rule.get("is_active", True)),
            "inactive": sum(1 for rule in rules if not rule.get("is_active", True)),
        },
    }


@app.post("/api/settings/rules")
async def api_create_category_rule(request: Request):
    """Create a categorization rule (contains/regex => category)."""
    body = await request.json()
    pattern = str(body.get("pattern") or "").strip()
    category = str(body.get("category") or "").strip()
    raw_rule_type = str(body.get("rule_type") or "contains")

    if not pattern:
        return JSONResponse({"error": "pattern is required"}, status_code=400)
    if not category:
        return JSONResponse({"error": "category is required"}, status_code=400)

    try:
        rule_type = normalize_rule_type(raw_rule_type)
    except ValueError as exc:
        return JSONResponse({"error": str(exc)}, status_code=400)

    if rule_type == "regex":
        import re

        try:
            re.compile(pattern)
        except re.error as exc:
            return JSONResponse({"error": f"Invalid regex: {exc}"}, status_code=400)

    with _get_db() as db:
        rule = db.add_category_rule(rule_type=rule_type, pattern=pattern, category=category)

    return {"ok": True, "rule": rule}


@app.patch("/api/settings/rules/{rule_id}")
async def api_update_category_rule(rule_id: int, request: Request):
    """Toggle or reorder a categorization rule."""
    body = await request.json()
    move = str(body.get("move") or "").strip().lower()
    is_active = body.get("is_active") if "is_active" in body else None

    with _get_db() as db:
        try:
            if move:
                rules = db.move_category_rule(rule_id, move)
                rule = next((item for item in rules if int(item["id"]) == int(rule_id)), None)
            else:
                rule = db.update_category_rule(
                    rule_id,
                    is_active=_coerce_bool(is_active) if is_active is not None else None,
                )
        except ValueError as exc:
            return JSONResponse({"error": str(exc)}, status_code=400)
        except KeyError:
            return JSONResponse({"error": "Rule not found"}, status_code=404)

    if not rule:
        return JSONResponse({"error": "Rule not found"}, status_code=404)
    return {"ok": True, "rule": rule}


@app.post("/api/settings/rules/test")
async def api_test_category_rule(request: Request):
    """Preview whether a rule would match sample text and historical rows."""
    body = await request.json()
    pattern = str(body.get("pattern") or "").strip()
    sample_text = str(body.get("sample_text") or "").strip()

    if not pattern:
        return JSONResponse({"error": "pattern is required"}, status_code=400)

    try:
        rule_type = normalize_rule_type(str(body.get("rule_type") or "contains"))
    except ValueError as exc:
        return JSONResponse({"error": str(exc)}, status_code=400)

    if rule_type == "regex":
        import re

        try:
            re.compile(pattern)
        except re.error as exc:
            return JSONResponse({"error": f"Invalid regex: {exc}"}, status_code=400)

    with _get_db() as db:
        preview = _simulate_rule_impact(
            db,
            rule_type=rule_type,
            pattern=pattern,
            sample_text=sample_text,
        )

    return {"ok": True, **preview}


@app.delete("/api/settings/rules/{rule_id}")
async def api_delete_category_rule(rule_id: int):
    """Delete a categorization rule by ID."""
    with _get_db() as db:
        deleted = db.delete_category_rule(rule_id)
    if not deleted:
        return JSONResponse({"error": "Rule not found"}, status_code=404)
    return {"ok": True, "id": rule_id}


@app.get("/api/settings/learning")
async def api_learning_summary():
    """Return recent learning events and summary counters."""
    with _get_db() as db:
        events = db.get_recent_learning_feedback(limit=40)
        feedback_count = db._conn.execute("SELECT COUNT(*) FROM learning_feedback").fetchone()[0]
        override_count = db._conn.execute("SELECT COUNT(*) FROM user_overrides").fetchone()[0]
        uncategorized_count = db._conn.execute(
            "SELECT COUNT(*) FROM tx_history WHERE category = 'Uncategorized'"
        ).fetchone()[0]
        learned_future_count = db._conn.execute(
            "SELECT COUNT(*) FROM learning_feedback WHERE apply_to_future = 1"
        ).fetchone()[0]

    return {
        "events": events,
        "summary": {
            "feedback_count": int(feedback_count),
            "override_count": int(override_count),
            "uncategorized_count": int(uncategorized_count),
            "learned_future_count": int(learned_future_count),
        },
    }


@app.post("/api/settings/learning/reapply")
async def api_reapply_learning():
    """Re-run deterministic learning on historical transactions."""
    with _get_db() as db:
        result = db.reapply_learning_to_history()
    return {"ok": True, **result}


@app.post("/api/settings/reset-db")
async def api_reset_db(request: Request):
    """Reset local SQLite data after explicit confirmation."""
    body = await request.json()
    if body.get("confirm") != "RESET":
        return JSONResponse(
            {"ok": False, "error": "Confirmation token missing"},
            status_code=400,
        )

    with _get_db() as db:
        deleted = db.reset_all_data()

    logger.warning("Local DB reset requested from settings page: %s", deleted)
    return {
        "ok": True,
        "message": "Local database reset completed",
        "deleted": deleted,
    }


# ── API: Upload & Process ────────────────────────────────────────


@app.post("/api/upload")
async def api_upload(file: UploadFile = File(...)):
    """Upload a supported file, parse & categorise, stream progress via SSE."""
    supported_file_types = [".csv", ".pdf", ".ofx"]
    import json as _json
    from fastapi.responses import StreamingResponse as _SR

    settings = load_settings()
    with BookmarkDB(settings.db_path) as db:
        if _build_setup_status(settings, db)["requires_base_currency_setup"]:
            return _base_currency_setup_response(include_ok=False)
        base_currency = _resolve_base_currency(settings, db)

    suffix = Path(file.filename or "upload.csv").suffix.lower()
    if suffix not in supported_file_types:
        supported = " or ".join(supported_file_types)
        return JSONResponse(
            {"error": f"Unsupported file type: {suffix}. Upload a {supported}"},
            status_code=400,
        )

    # Read upload content now (before streaming response starts)
    file_bytes = await file.read()

    async def _stream():
        def evt(pct: int, step: str, **extra) -> str:
            payload = _json.dumps({"pct": pct, "step": step, **extra})
            return f"data: {payload}\n\n"

        try:
            # ── Phase 1: save to temp ──────────────────────────────
            yield evt(5, "Saving file...")
            import asyncio, tempfile, shutil
            with tempfile.NamedTemporaryFile(delete=False, suffix=suffix) as tmp:
                tmp.write(file_bytes)
                tmp_path = tmp.name

            # ── Phase 2: parse ─────────────────────────────────────
            yield evt(15, "Parsing transactions...")
            await asyncio.sleep(0)  # yield control so event is flushed
            try:
                if suffix == ".pdf":
                    from spectra.pdf_parser import parse_pdf
                    parsed = parse_pdf(tmp_path, currency=base_currency)
                elif suffix == ".csv":
                    from spectra.csv_parser import parse_csv
                    parsed = parse_csv(tmp_path, currency=base_currency)
                else:
                    from spectra.ofx_parser import parse_ofx
                    parsed = parse_ofx(tmp_path, currency=base_currency)
            finally:
                Path(tmp_path).unlink(missing_ok=True)

            # ── Phase 3: dedup ─────────────────────────────────────
            yield evt(25, "Checking for duplicates...")
            await asyncio.sleep(0)
            with _get_db() as db:
                new_txns = [t for t in parsed if not db.is_seen(t.id)]
                overrides = db.get_overrides()
                category_rules = db.get_category_rules()
                merchant_db = db.get_merchant_categories()
                training_data = db.get_training_data()
                import_batch_id = build_import_batch_id()

            if not new_txns:
                yield evt(100, "All transactions already imported", done=True,
                          transactions=[], message="All transactions already imported")
                return

            n = len(new_txns)

            # ── Phase 4: categorise (25% → 92%, per transaction) ───
            # Pre-categorise from overrides (instant)
            pre_cat = []
            to_process = []
            override_count = 0
            rule_count = 0
            for t in new_txns:
                od = t.raw_description
                counterpart = str(getattr(t, "counterpart", "") or "")
                if od in overrides:
                    pre_cat.append(CategorisedTransaction(
                        id=t.id, original_description=od,
                        clean_name=overrides[od]["clean_name"],
                        category=overrides[od]["category"],
                        amount=t.amount, currency=t.currency, date=t.date,
                        counterpart=counterpart,
                        classification_source="override",
                        account_name=str(getattr(t, "account_name", "") or ""),
                        import_batch_id=import_batch_id,
                    ))
                    override_count += 1
                    continue

                from spectra.rules import first_matching_rule

                matched_rule = first_matching_rule(
                    category_rules,
                    clean_name=counterpart or od,
                    raw_description=od,
                )
                if matched_rule:
                    pre_cat.append(CategorisedTransaction(
                        id=t.id,
                        original_description=od,
                        clean_name=counterpart or od,
                        category=str(matched_rule["category"]),
                        amount=t.amount,
                        currency=t.currency,
                        date=t.date,
                        counterpart=counterpart,
                        classification_source="rule",
                        account_name=str(getattr(t, "account_name", "") or ""),
                        import_batch_id=import_batch_id,
                    ))
                    rule_count += 1
                else:
                    to_process.append(t)

            if override_count or rule_count:
                yield evt(
                    28,
                    f"Applied local mappings: {override_count} overrides, {rule_count} rules",
                )

            categorised = list(pre_cat)

            if to_process:
                flat = [
                    {
                        "id": t.id,
                        "raw_description": t.raw_description,
                        "counterpart": getattr(t, "counterpart", ""),
                        "statement_category": getattr(t, "statement_category", ""),
                        "amount": t.amount,
                        "currency": t.currency,
                        "date": t.date,
                    }
                    for t in to_process
                ]

                if settings.ai_provider == "local":
                    from spectra.local_categorizer import categorise_local
                    from spectra.ml_classifier import train_classifier
                    ml_clf = train_classifier(training_data)

                    # Categorise one-by-one so we can stream real progress
                    results = []
                    for i, row in enumerate(flat):
                        pct = 25 + int((i + 1) / len(flat) * 67)
                        yield evt(pct, f"Categorizing {i + 1} / {len(flat)}...")
                        await asyncio.sleep(0)
                        r = categorise_local([row], merchant_db=merchant_db, ml_classifier=ml_clf)
                        results.extend(r)
                    categorised.extend(results)

                else:
                    # Cloud: categorise in one batch (can't stream per-row)
                    from spectra.ai import categorise
                    provider = settings.ai_provider
                    if provider == "gemini":
                        api_key, model = settings.gemini_api_key, settings.gemini_model
                    else:
                        api_key, model = settings.openai_api_key, settings.openai_model

                    # Fake granular progress while waiting for API
                    for pct in range(30, 88, 5):
                        yield evt(pct, f"Waiting for {provider.title()} AI...")
                        await asyncio.sleep(0.4)

                    results = categorise(flat, [], provider=provider,
                                         api_key=api_key, model=model,
                                         base_currency=base_currency)
                    categorised.extend(results)

            _enrich_categorised_transactions(
                categorised,
                source_transactions=new_txns,
                import_batch_id=import_batch_id,
                provider=settings.ai_provider,
            )

            # ── Phase 5: recurring detection ───────────────────────
            yield evt(94, "Detecting recurring payments...")
            await asyncio.sleep(0)
            with _get_db() as db:
                history = db.get_merchant_history()
            from spectra.recurring import apply_recurring_tags
            apply_recurring_tags(categorised, history)

            # ── Phase 6: FX conversion ─────────────────────────────
            yield evt(97, "Converting currencies...")
            await asyncio.sleep(0)
            from spectra.fx import convert_currency
            for t in categorised:
                if t.currency.upper() != base_currency:
                    orig_amt, orig_cur = t.amount, t.currency.upper()
                    t.amount = convert_currency(orig_amt, orig_cur, base_currency, t.date)
                    t.original_amount, t.original_currency = orig_amt, orig_cur
                    t.currency = base_currency

            for t in categorised:
                apply_review_state(t)

            with _get_db() as db:
                existing_transfer_candidates = _load_existing_transfer_candidates(db, categorised)
            match_internal_transfers(categorised, existing_candidates=existing_transfer_candidates)

            # ── Done ───────────────────────────────────────────────
            preview = [_serialize_transaction_model(t) for t in categorised]
            review_count = sum(1 for item in categorised if item.needs_review)
            transfer_count = sum(1 for item in categorised if item.transfer_status == TRANSFER_STATUS_SUGGESTED)
            yield evt(100, f"{len(preview)} transactions ready", done=True,
                      transactions=preview,
                      message=f"{len(preview)} new transactions",
                      review_count=review_count,
                      transfer_count=transfer_count)

        except Exception as e:
            logger.exception("Upload stream error: %s", e)
            yield f"data: {_json.dumps({'pct': 0, 'step': 'Error: ' + str(e), 'error': True})}\n\n"

    return _SR(_stream(), media_type="text/event-stream", headers={
        "Cache-Control": "no-cache",
        "X-Accel-Buffering": "no",   # disable nginx buffering if behind proxy
    })



@app.post("/api/confirm")
async def api_confirm(request: Request):
    """Confirm and save previously previewed transactions to the DB."""
    body = await request.json()
    transactions = body.get("transactions", [])

    if not transactions:
        return {"ok": False, "message": "No transactions to save"}

    settings = load_settings()
    with BookmarkDB(settings.db_path) as db:
        if _build_setup_status(settings, db)["requires_base_currency_setup"]:
            return _base_currency_setup_response(include_ok=True)
        base_currency = _resolve_base_currency(settings, db)

    with _get_db() as db:
        cats: list[CategorisedTransaction] = []
        future_mappings: dict[str, str] = {}
        future_overrides: dict[str, dict[str, str]] = {}
        learned_count = 0
        if transactions and not transactions[0].get("import_batch_id"):
            fallback_batch_id = build_import_batch_id()
        else:
            fallback_batch_id = ""
        for t in transactions:
            if fallback_batch_id and not t.get("import_batch_id"):
                t["import_batch_id"] = fallback_batch_id
            ct = _payload_to_transaction(t, base_currency=base_currency)
            apply_review_state(ct)
            cats.append(ct)

            apply_to_future = _coerce_bool(t.get("apply_to_future"), True)
            _persist_learning(
                db,
                tx_id=str(ct.id),
                original_description=str(ct.original_description),
                clean_name=str(ct.clean_name),
                category=str(ct.category),
                source="upload_confirm",
                apply_to_future=apply_to_future,
            )
            if apply_to_future and ct.category != "Uncategorized":
                future_mappings[ct.clean_name] = ct.category
                if ct.original_description:
                    future_overrides[ct.original_description] = {
                        "clean_name": ct.clean_name,
                        "category": ct.category,
                    }
                learned_count += 1

        existing_transfer_candidates = _load_existing_transfer_candidates(db, cats)
        existing_transfer_updates = match_internal_transfers(
            cats,
            existing_candidates=existing_transfer_candidates,
        )
        db.save_history(cats)
        for update in existing_transfer_updates:
            tx_id = str(update.pop("tx_id"))
            db.update_transaction(tx_id, **update)
        db.save_merchant_categories_batch(future_mappings)
        db.save_overrides(future_overrides)
        review_count = sum(1 for tx in cats if tx.needs_review)
        transfer_count = sum(1 for tx in cats if tx.transfer_status == TRANSFER_STATUS_SUGGESTED)
        redirect_to = "/review" if review_count else "/transactions"

        # Optionally sync to Google Sheets
        if settings.spreadsheet_id and (settings.google_sheets_credentials_b64 or
                                         Path(settings.google_sheets_credentials_file).exists()):
            try:
                from spectra.sheets import SheetsClient
                sheets = SheetsClient(
                    spreadsheet_id=settings.spreadsheet_id,
                    credentials_b64=settings.google_sheets_credentials_b64,
                    credentials_file=settings.google_sheets_credentials_file,
                )
                sheets.append_transactions(cats)
                from spectra.dashboard import refresh_dashboard
                refresh_dashboard(sheets)
                return {
                    "ok": True,
                    "message": f"Saved {len(cats)} transactions + synced to Sheets · {review_count} need review · {transfer_count} transfer suggestion(s) · learned {learned_count} future mapping(s)",
                    "review_count": review_count,
                    "transfer_count": transfer_count,
                    "redirect_to": redirect_to,
                }
            except Exception as e:
                logger.warning("Sheets sync failed: %s", e)
                return {
                    "ok": True,
                    "message": f"Saved {len(cats)} transactions · {review_count} need review · {transfer_count} transfer suggestion(s) · learned {learned_count} future mapping(s) (Sheets sync failed)",
                    "review_count": review_count,
                    "transfer_count": transfer_count,
                    "redirect_to": redirect_to,
                }

    return {
        "ok": True,
        "message": f"Saved {len(cats)} transactions to local DB · {review_count} need review · {transfer_count} transfer suggestion(s) · learned {learned_count} future mapping(s)",
        "review_count": review_count,
        "transfer_count": transfer_count,
        "redirect_to": redirect_to,
    }



# ── Pages: Budget & Trends ───────────────────────────────────────


@app.get("/budget", response_class=HTMLResponse)
async def page_budget(request: Request):
    if (redirect := _setup_redirect_if_needed(request)):
        return redirect
    return templates.TemplateResponse(request, "budget.html", _template_context(request))


@app.get("/trends", response_class=HTMLResponse)
async def page_trends(request: Request):
    if (redirect := _setup_redirect_if_needed(request)):
        return redirect
    return templates.TemplateResponse(request, "trends.html", _template_context(request))


# ── API: Budget ──────────────────────────────────────────────────


@app.get("/api/budget")
async def api_budget():
    """Return per-category budget status for the current month."""
    preferences = _load_app_preferences()
    current_cycle = _build_cycle_payload(preferences["cycle_rule"])

    with _get_db() as db:
        # All expense rows for the current financial cycle
        rows = db._conn.execute(
            """
            SELECT category, SUM(amount) as total
            FROM tx_history
            WHERE amount < 0 AND excluded_from_spend = 0 AND date >= ? AND date < ?
            GROUP BY category
            """,
            (current_cycle["start"], current_cycle["end"]),
        ).fetchall()

        limits = db.get_budget_limits()

        # All-time categories so we can show unspent ones too
        all_cats = db._conn.execute(
            """
            SELECT DISTINCT category FROM tx_history
            WHERE category != 'Uncategorized' AND amount < 0 AND excluded_from_spend = 0
            ORDER BY category
            """
        ).fetchall()

    spent_by_cat: dict[str, float] = {cat: abs(total) for cat, total in rows}
    categories = sorted({row[0] for row in all_cats} | set(limits.keys()))

    items = []
    for cat in categories:
        spent = round(spent_by_cat.get(cat, 0.0), 2)
        limit = limits.get(cat)
        if limit and limit > 0:
            pct = round(spent / limit * 100, 1)
            if pct >= 100:
                status = "red"
            elif pct >= 80:
                status = "yellow"
            else:
                status = "green"
        else:
            pct = None
            status = "none"

        items.append({
            "category": cat,
            "spent": spent,
            "limit": limit,
            "pct": pct,
            "status": status,
        })

    # Sort: over-budget first, then by spent desc
    items.sort(key=lambda x: (x["status"] != "red", x["status"] != "yellow", -x["spent"]))

    on_track = sum(1 for i in items if i["status"] == "green")
    over = sum(1 for i in items if i["status"] == "red")
    no_limit = sum(1 for i in items if i["status"] == "none")

    return {
        "current_cycle": current_cycle,
        "items": items,
        "summary": {"on_track": on_track, "over": over, "no_limit": no_limit},
    }


@app.patch("/api/budget/{category}")
async def api_update_budget(category: str, request: Request):
    """Save or update a monthly budget limit for a category."""
    body = await request.json()
    limit = body.get("limit")
    if limit is None or limit < 0:
        return JSONResponse({"error": "limit must be a non-negative number"}, status_code=400)

    with _get_db() as db:
        db.save_budget_limit(category, float(limit))

    return {"ok": True, "category": category, "limit": limit}


# ── API: Trends ──────────────────────────────────────────────────


@app.get("/api/trends")
async def api_trends():
    """Return year-over-year financial data for the Trends page."""
    from collections import defaultdict

    preferences = _load_app_preferences()
    cycle_rule = preferences["cycle_rule"]

    with _get_db() as db:
        rows = db._conn.execute(
            "SELECT date, amount, category FROM tx_history WHERE excluded_from_spend = 0 ORDER BY date ASC"
        ).fetchall()

    if not rows:
        return {"years": [], "by_year": {}, "period_series": [], **preferences}

    # Aggregate by year and month
    by_year: dict[str, dict] = {}
    monthly_income: dict[str, float] = defaultdict(float)
    monthly_expense: dict[str, float] = defaultdict(float)
    cat_by_year: dict[str, dict[str, float]] = defaultdict(lambda: defaultdict(float))

    for date_str, amount, category in rows:
        tx_date = parse_iso_date(date_str)
        cycle_start = cycle_start_for(tx_date, cycle_rule)
        year = str(cycle_start.year)
        month = cycle_start.isoformat()

        if year not in by_year:
            by_year[year] = {"income": 0.0, "expenses": 0.0}

        if amount > 0:
            by_year[year]["income"] += amount
            monthly_income[month] += amount
        else:
            by_year[year]["expenses"] += abs(amount)
            monthly_expense[month] += abs(amount)
            if category != "Uncategorized":
                cat_by_year[year][category] += abs(amount)

    # Compute net flow and savings rate per year
    years = sorted(by_year.keys())
    year_stats = {}
    for y in years:
        inc = round(by_year[y]["income"], 2)
        exp = round(by_year[y]["expenses"], 2)
        net = round(inc - exp, 2)
        savings_rate = round((net / inc * 100), 1) if inc > 0 else 0.0
        year_stats[y] = {
            "income": inc,
            "expenses": exp,
            "net": net,
            "savings_rate": savings_rate,
            "by_category": {k: round(v, 2) for k, v in sorted(
                cat_by_year[y].items(), key=lambda x: -x[1]
            )[:8]},  # top 8 categories
        }

    # Build monthly series for charts (all years combined, keyed by YYYY-MM)
    all_months = sorted(set(monthly_income) | set(monthly_expense))
    monthly_series = [
        {
            "period_start": m,
            "period_end": next_cycle_start(parse_iso_date(m), cycle_rule).isoformat(),
            "income": round(monthly_income.get(m, 0), 2),
            "expenses": round(monthly_expense.get(m, 0), 2),
        }
        for m in all_months
    ]

    return {
        "years": years,
        "by_year": year_stats,
        "period_series": monthly_series,
        **preferences,
    }


@app.get("/api/subscriptions")
async def api_subscriptions():
    """Return recurring subscriptions with monthly and annual projections."""
    from collections import defaultdict
    from datetime import date, timedelta
    from statistics import median

    preferences = _load_app_preferences()
    cycle_start, cycle_end = cycle_window_for(date.today(), preferences["cycle_rule"])

    with _get_db() as db:
        rows = db._conn.execute(
            """
            SELECT date, clean_name, amount, category, COALESCE(original_description, '')
            FROM tx_history
            WHERE excluded_from_spend = 0
            ORDER BY date ASC
            """
        ).fetchall()

    buckets: dict[str, dict[str, Any]] = defaultdict(
        lambda: {
            "name": "",
            "dates": [],
            "amounts": [],
            "in_cycle": 0.0,
            "last_date": "",
            "category": "Digital Subscriptions",
        }
    )

    for date_str, clean_name, amount, category, original_description in rows:
        if amount >= 0:
            continue

        recurring_kind = detect_recurring_kind(clean_name, original_description, amount)
        if recurring_kind != "Subscription" and category != "Digital Subscriptions":
            continue

        tx_date = parse_iso_date(date_str)
        bucket = buckets[clean_name]
        bucket["name"] = clean_name
        bucket["category"] = category or "Digital Subscriptions"
        bucket["dates"].append(tx_date)
        bucket["amounts"].append(abs(float(amount)))
        if cycle_start <= tx_date < cycle_end:
            bucket["in_cycle"] += abs(float(amount))
        if date_str > bucket["last_date"]:
            bucket["last_date"] = date_str

    items: list[dict[str, Any]] = []
    monthly_total = 0.0
    price_change_count = 0

    for merchant, bucket in buckets.items():
        dates = sorted(set(bucket["dates"]))
        amounts = bucket["amounts"]
        if not amounts:
            continue

        avg_amount = sum(amounts) / len(amounts)
        intervals = [
            (dates[i] - dates[i - 1]).days
            for i in range(1, len(dates))
            if (dates[i] - dates[i - 1]).days > 0
        ]
        cadence_days = int(round(median(intervals))) if intervals else 30
        cadence_days = max(cadence_days, 1)

        monthly_estimate = avg_amount * (30.4375 / cadence_days)
        annual_projection = monthly_estimate * 12
        monthly_total += monthly_estimate

        last_date = parse_iso_date(bucket["last_date"]) if bucket["last_date"] else dates[-1]
        next_charge = last_date + timedelta(days=cadence_days)
        previous_amounts = amounts[:-1]
        baseline_amount = median(previous_amounts) if previous_amounts else amounts[-1]
        change_amount = round(amounts[-1] - baseline_amount, 2)
        change_pct = round((change_amount / baseline_amount) * 100, 1) if baseline_amount else 0.0
        price_change_direction = ""
        if previous_amounts and abs(change_amount) >= max(1.0, baseline_amount * 0.08):
            price_change_direction = "up" if change_amount > 0 else "down"
            price_change_count += 1

        items.append(
            {
                "merchant": merchant,
                "category": bucket["category"],
                "last_amount": round(amounts[-1], 2),
                "average_amount": round(avg_amount, 2),
                "cadence_days": cadence_days,
                "last_charge_date": last_date.isoformat(),
                "next_estimated_date": next_charge.isoformat(),
                "monthly_estimate": round(monthly_estimate, 2),
                "annual_projection": round(annual_projection, 2),
                "in_current_cycle": round(bucket["in_cycle"], 2),
                "payments_count": len(amounts),
                "price_change_direction": price_change_direction,
                "change_amount": change_amount,
                "change_pct": change_pct,
            }
        )

    items.sort(key=lambda row: row["monthly_estimate"], reverse=True)

    return {
        "items": items,
        "summary": {
            "active_count": len(items),
            "monthly_estimate": round(monthly_total, 2),
            "annual_projection": round(monthly_total * 12, 2),
            "in_current_cycle": round(sum(item["in_current_cycle"] for item in items), 2),
            "price_change_count": price_change_count,
        },
        "current_cycle": {
            "start": cycle_start.isoformat(),
            "end": cycle_end.isoformat(),
            "label": format_cycle_label(cycle_start, cycle_end),
        },
    }


# ── Launch ───────────────────────────────────────────────────────


def serve(host: str = "127.0.0.1", port: int = 8080) -> None:
    """Launch the Spectra dashboard server."""
    import uvicorn
    print(f"\n  🌟 Spectra Dashboard running at http://{host}:{port}\n")
    uvicorn.run(app, host=host, port=port, log_level="warning")
