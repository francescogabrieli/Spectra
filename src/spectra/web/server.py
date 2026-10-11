"""Spectra Web Dashboard — FastAPI backend."""

from __future__ import annotations

import logging
import csv
import io
import re
import threading
from datetime import date
from decimal import Decimal
from pathlib import Path
from typing import Any

from fastapi import FastAPI, File, Query, Request, UploadFile
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from starlette.concurrency import run_in_threadpool
from starlette.exceptions import HTTPException as StarletteHTTPException
from starlette.background import BackgroundTask

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
from spectra.services.clients import (
    EnrichmentClient, ImportClient, LedgerClient, ServiceResponseError, ServiceUnavailable,
)
from spectra.ml_classifier import build_seed_data
from spectra.money import as_decimal, currency_exponent, decimal_text, quantize_money
from spectra.recurring import detect_recurring_kind
from spectra.review import (
    TRANSFER_STATUS_CONFIRMED,
    TRANSFER_STATUS_DISMISSED,
    TRANSFER_STATUS_NONE,
    TRANSFER_STATUS_SUGGESTED,
    apply_review_state,
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
    "amount_decimal",
    "amount_minor",
    "original_amount",
    "original_currency",
    "original_amount_decimal",
    "original_amount_minor",
    "conversion_rate",
    "conversion_date",
    "conversion_source",
    "conversion_error",
    "currency_source",
    "original_description",
    "counterpart",
    "recurring",
    "classification_source",
    "category_confidence",
    "needs_review",
    "review_reason",
    "account_name",
    "account_id",
    "import_batch_id",
    "transfer_group_id",
    "transfer_status",
    "excluded_from_spend",
]
_UPLOAD_IMPORT_SLOTS = threading.BoundedSemaphore(2)


@app.get("/health/live")
async def health_live() -> dict[str, str]:
    return {"service": "gateway", "status": "live"}


@app.get("/health/ready")
async def health_ready() -> dict[str, Any]:
    """Readiness requires each API the gateway routes to be reachable."""
    import httpx

    settings = load_settings()
    dependencies = {
        "ledger": f"{settings.ledger_url.rstrip('/')}/health/ready",
        "importer": f"{settings.import_service_url.rstrip('/')}/health/ready",
        "enrichment": f"{settings.enrichment_url.rstrip('/')}/health/ready",
    }
    states: dict[str, str] = {}
    async with httpx.AsyncClient(timeout=2.0) as client:
        for name, url in dependencies.items():
            try:
                response = await client.get(url)
                response.raise_for_status()
                states[name] = "ready"
            except httpx.HTTPError:
                states[name] = "unavailable"
    if any(state != "ready" for state in states.values()):
        return JSONResponse(
            {"service": "gateway", "status": "not_ready", "dependencies": states},
            status_code=503,
        )
    return {"service": "gateway", "status": "ready", "dependencies": states}


# ── Global error handler ─────────────────────────────────────────


@app.exception_handler(StarletteHTTPException)
async def http_exception_handler(request, exc):
    return JSONResponse({"error": str(exc.detail)}, status_code=exc.status_code)


@app.exception_handler(Exception)
async def generic_exception_handler(request, exc):
    logger.exception("Unhandled error: %s", exc)
    return JSONResponse({"error": "Internal server error"}, status_code=500)


@app.exception_handler(ServiceUnavailable)
async def service_unavailable_handler(request, exc: ServiceUnavailable):
    return JSONResponse(
        {"error": str(exc), "service_unavailable": True},
        status_code=503,
    )

def _get_db() -> LedgerClient:
    settings = load_settings()
    return LedgerClient(settings)


def _load_app_preferences(db: LedgerClient | None = None) -> dict[str, Any]:
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


def _build_cycle_burn_rate(
    *, today, period_start, period_end, total_spent: Decimal, currency: str,
) -> dict[str, Any]:
    """Estimate end-of-cycle spend based on the current daily burn."""
    total_days = max((period_end - period_start).days, 1)
    elapsed_days = min(max((today - period_start).days + 1, 1), total_days)
    remaining_days = max(total_days - elapsed_days, 0)
    total_spent = as_decimal(total_spent)
    daily_spend = total_spent / Decimal(elapsed_days)
    projected_total = daily_spend * total_days
    spent_exact = quantize_money(total_spent, currency)
    daily_exact = quantize_money(daily_spend, currency)
    projected_exact = quantize_money(projected_total, currency)

    return {
        "elapsed_days": elapsed_days,
        "total_days": total_days,
        "remaining_days": remaining_days,
        "spent_so_far": float(spent_exact),
        "spent_so_far_decimal": decimal_text(spent_exact, currency),
        "daily_spend": float(daily_exact),
        "daily_spend_decimal": decimal_text(daily_exact, currency),
        "projected_total": float(projected_exact),
        "projected_total_decimal": decimal_text(projected_exact, currency),
    }


def _normalize_currency_code(value: str | None) -> str | None:
    code = str(value or "").strip().upper()
    if not code:
        return None
    if not _CURRENCY_CODE_RE.fullmatch(code):
        return None
    return code


def _resolve_base_currency(settings: Settings, db: LedgerClient) -> str:
    stored = _normalize_currency_code(db.get_app_setting(_BASE_CURRENCY_SETTING_KEY))
    if stored:
        return stored
    from_env = _normalize_currency_code(settings.base_currency)
    return from_env or "EUR"


def _provider_is_ready(settings: Settings) -> bool:
    if settings.ai_provider == "local":
        return True
    try:
        with EnrichmentClient(settings) as enrichment:
            status = enrichment.provider_status()
    except ServiceUnavailable:
        return False
    return bool(status.get("ready"))


def _build_setup_status(settings: Settings, db: LedgerClient) -> dict[str, Any]:
    stored_base_currency = _normalize_currency_code(db.get_app_setting(_BASE_CURRENCY_SETTING_KEY))
    effective_currency = _resolve_base_currency(settings, db)
    ledger_summary = db.read_model("setup_summary")
    tx_count = int(ledger_summary["transaction_count"])
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


def _requires_base_currency_setup(db: LedgerClient) -> bool:
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
    with LedgerClient(settings) as db:
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
            _csv_safe_value(row.get("amount_decimal")),
            _csv_safe_value(row.get("amount_minor")),
            _csv_safe_value(row.get("original_amount")),
            _csv_safe_value(row.get("original_currency")),
            _csv_safe_value(row.get("original_amount_decimal")),
            _csv_safe_value(row.get("original_amount_minor")),
            _csv_safe_value(row.get("conversion_rate")),
            _csv_safe_value(row.get("conversion_date")),
            _csv_safe_value(row.get("conversion_source")),
            _csv_safe_value(row.get("conversion_error")),
            _csv_safe_value(row.get("currency_source")),
            _csv_safe_value(row.get("original_description")),
            _csv_safe_value(row.get("counterpart")),
            _csv_safe_value(row.get("recurring")),
            _csv_safe_value(row.get("classification_source")),
            _csv_safe_value(row.get("category_confidence")),
            _csv_safe_value(int(bool(row.get("needs_review")))),
            _csv_safe_value(row.get("review_reason")),
            _csv_safe_value(row.get("account_name")),
            _csv_safe_value(row.get("account_id")),
            _csv_safe_value(row.get("import_batch_id")),
            _csv_safe_value(row.get("transfer_group_id")),
            _csv_safe_value(row.get("transfer_status")),
            _csv_safe_value(int(bool(row.get("excluded_from_spend")))),
        ])
    return buffer.getvalue()


def _persist_learning(
    db: LedgerClient,
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


def _refresh_review_state(db: LedgerClient, tx_id: str) -> dict[str, Any] | None:
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
        "amount_decimal": tx.amount_decimal,
        "recurring": tx.recurring,
        "original_description": tx.original_description,
        "counterpart": tx.counterpart,
        "classification_source": tx.classification_source,
        "category_confidence": tx.category_confidence,
        "needs_review": tx.needs_review,
        "review_reason": tx.review_reason,
        "account_name": tx.account_name,
        "account_id": tx.account_id,
        "account_balance": tx.account_balance,
        "account_balance_date": tx.account_balance_date,
        "currency_source": tx.currency_source,
        "import_batch_id": tx.import_batch_id,
        "transfer_group_id": tx.transfer_group_id,
        "transfer_status": tx.transfer_status,
        "excluded_from_spend": tx.excluded_from_spend,
        "original_amount": tx.original_amount,
        "original_currency": tx.original_currency,
        "original_amount_decimal": tx.original_amount_decimal,
        "conversion_rate": tx.conversion_rate,
        "conversion_date": tx.conversion_date,
        "conversion_source": tx.conversion_source,
        "conversion_error": tx.conversion_error,
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
        original_amount_decimal=payload.get("original_amount_decimal"),
        conversion_rate=payload.get("conversion_rate"),
        conversion_date=payload.get("conversion_date"),
        conversion_source=str(payload.get("conversion_source", "") or ""),
        conversion_error=str(payload.get("conversion_error", "") or ""),
        amount_decimal=payload.get("amount_decimal"),
        date=str(payload.get("date", "") or ""),
        statement_category=str(payload.get("statement_category", "") or ""),
        recurring=str(payload.get("recurring", "") or ""),
        counterpart=str(payload.get("counterpart", "") or ""),
        classification_source=str(payload.get("classification_source", "") or ""),
        category_confidence=payload.get("category_confidence"),
        needs_review=bool(payload.get("needs_review", False)),
        review_reason=str(payload.get("review_reason", "") or ""),
        account_name=str(payload.get("account_name", "") or ""),
        account_id=str(payload.get("account_id", "") or ""),
        account_balance=payload.get("account_balance"),
        account_balance_date=str(payload.get("account_balance_date", "") or ""),
        currency_source=str(payload.get("currency_source", "parser") or "parser"),
        import_batch_id=str(payload.get("import_batch_id", "") or ""),
        transfer_group_id=str(payload.get("transfer_group_id", "") or ""),
        transfer_status=str(payload.get("transfer_status", TRANSFER_STATUS_NONE) or TRANSFER_STATUS_NONE),
        excluded_from_spend=bool(payload.get("excluded_from_spend", False)),
    )


def _simulate_rule_impact(
    db: LedgerClient,
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
    rows = db.read_model("rule_impact_rows")

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
    rows: list[tuple[str, str, Decimal, str]],
    period_start,
    period_end,
    cycle_rule: str,
    total_spent: Decimal,
    uncategorized: int,
    burn_rate: dict[str, Any] | None,
    budget_limits: dict[str, Decimal],
    currency: str = "EUR",
) -> list[dict[str, str]]:
    from collections import defaultdict
    from datetime import timedelta
    from statistics import median

    def fmt(amount: Decimal) -> str:
        amount = quantize_money(amount, currency)
        return f"{currency} {amount:,.{currency_exponent(currency)}f}".replace(",", "_").replace(".", ",").replace("_", ".")

    insights: list[dict[str, str]] = []

    if scope == "cycle" and burn_rate:
        total_budget = sum(
            (limit for limit in budget_limits.values() if limit and limit > 0), Decimal(0),
        )
        if total_budget > 0:
            projected_total = as_decimal(
                burn_rate.get("projected_total_decimal", burn_rate.get("projected_total", 0))
            )
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

    current_by_category: dict[str, Decimal] = defaultdict(Decimal)
    previous_by_category: dict[str, Decimal] = defaultdict(Decimal)
    history_by_merchant: dict[str, list[Decimal]] = defaultdict(list)
    current_expenses: list[tuple[str, Decimal, str]] = []
    subscription_by_merchant: dict[str, list[tuple[object, Decimal]]] = defaultdict(list)

    previous_cycle_start = cycle_start_for(period_start - timedelta(days=1), cycle_rule)
    previous_cycle_end = period_start

    for tx_date_str, clean_name, amount, category in rows:
        tx_date = parse_iso_date(tx_date_str)
        if amount >= 0:
            continue

        spend = abs(as_decimal(amount))
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
        previous_total = previous_by_category.get(category_name, Decimal(0))
        delta = current_total - previous_total
        if delta <= max(Decimal(25), previous_total * Decimal("0.25")):
            continue
        if biggest_delta is None or delta > biggest_delta[1]:
            biggest_delta = (category_name, delta, previous_total, current_total)

    if biggest_delta:
        category_name, delta, previous_total, current_total = biggest_delta
        previous_text = fmt(previous_total) if previous_total else fmt(0)
        insights.append(
            {
                "type": "category_delta",
                "severity": "info",
                "title": f"{category_name} is the main driver this cycle",
                "detail": f"Up by {fmt(delta)} versus the previous cycle ({fmt(current_total)} vs {previous_text}).",
                "href": "/trends",
            }
        )

    anomalies: list[tuple[str, Decimal, Decimal]] = []
    first_time_large: list[tuple[str, Decimal]] = []
    for merchant, spend, _category_name in current_expenses:
        past_amounts = history_by_merchant.get(merchant, [])
        if len(past_amounts) >= 2:
            baseline = median(past_amounts)
            if baseline > 0 and spend > baseline * Decimal("1.8") and (spend - baseline) >= 20:
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

    subscription_changes: list[tuple[str, Decimal, Decimal]] = []
    for merchant, history in subscription_by_merchant.items():
        history.sort(key=lambda item: item[0])
        if len(history) < 2:
            continue
        latest_date, latest_amount = history[-1]
        if not (period_start <= latest_date < period_end):
            continue

        previous_amounts = [amount for _dt, amount in history[:-1]]
        baseline = median(previous_amounts) if previous_amounts else Decimal(0)
        diff = latest_amount - baseline
        if baseline > 0 and abs(diff) >= max(Decimal(1), baseline * Decimal("0.08")):
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
def page_dashboard(request: Request):
    if (redirect := _setup_redirect_if_needed(request)):
        return redirect
    return templates.TemplateResponse(request, "dashboard.html", _template_context(request))


@app.get("/transactions", response_class=HTMLResponse)
def page_transactions(request: Request):
    if (redirect := _setup_redirect_if_needed(request)):
        return redirect
    return templates.TemplateResponse(request, "transactions.html", _template_context(request))


@app.get("/upload", response_class=HTMLResponse)
def page_upload(request: Request):
    if (redirect := _setup_redirect_if_needed(request)):
        return redirect
    return templates.TemplateResponse(request, "upload.html", _template_context(request))


@app.get("/settings", response_class=HTMLResponse)
def page_settings(request: Request):
    return templates.TemplateResponse(request, "settings.html", _template_context(request))


@app.get("/review", response_class=HTMLResponse)
def page_review(request: Request):
    if (redirect := _setup_redirect_if_needed(request)):
        return redirect
    return templates.TemplateResponse(request, "review.html", _template_context(request))


@app.get("/subscriptions", response_class=HTMLResponse)
def page_subscriptions(request: Request):
    if (redirect := _setup_redirect_if_needed(request)):
        return redirect
    return templates.TemplateResponse(request, "subscriptions.html", _template_context(request))


# ── API: Dashboard Summary ───────────────────────────────────────


@app.get("/api/summary")
def api_summary(scope: str = Query("cycle")):
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

    burn_rate = None

    with _get_db() as db:
        base_currency = _resolve_base_currency(load_settings(), db)
        rows = db.read_model("summary_rows", currency=base_currency)
        non_base_currency_count = int(db.read_model(
            "summary_non_base_count", start=period_start.isoformat(),
            end=period_end.isoformat(), currency=base_currency,
        ))
        budget_limits = db.get_budget_limits_exact(base_currency)
        needs_review_count = db.count_review_queue()
        confirmed_internal_transfers_count = db.count_confirmed_transfer_groups()

    rows = [
        (tx_date, merchant, as_decimal(amount), category)
        for tx_date, merchant, amount, category in rows
    ]

    if not rows:
        if scope == "cycle":
            burn_rate = _build_cycle_burn_rate(
                today=today,
                period_start=period_start,
                period_end=period_end,
                total_spent=Decimal(0),
                currency=base_currency,
            )
        return {
            "total_spent": 0, "total_income": 0, "subscriptions": 0,
            "uncategorized": 0, "uncategorized_total": 0, "by_category": {}, "monthly": {},
            "monthly_ranges": {}, "top_merchants": [], "current_cycle": _build_cycle_payload(cycle_rule),
            "scope": scope, "scope_label": scope_label,
            "base_currency": base_currency,
            "non_base_currency_count": non_base_currency_count,
            "selected_period": {"start": period_start.isoformat(), "end": period_end.isoformat(), "label": scope_label},
            **preferences,
            "has_data": non_base_currency_count > 0,
            "burn_rate": burn_rate,
            "insights": [],
            "needs_review_count": needs_review_count,
            "confirmed_internal_transfers_count": confirmed_internal_transfers_count,
        }

    from collections import Counter, defaultdict

    total_spent = Decimal(0)
    total_income = Decimal(0)
    subscriptions = Decimal(0)
    uncategorized = 0
    uncategorized_total = sum(1 for _d, _m, _a, cat in rows if str(cat) == "Uncategorized")
    by_category: dict[str, Decimal] = defaultdict(Decimal)
    monthly: dict[str, Decimal] = defaultdict(Decimal)
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

    for tx_date_str, clean_name, amount_text, cat in rows:
        tx_date = parse_iso_date(tx_date_str)
        if not (period_start <= tx_date < period_end):
            continue

        amount = as_decimal(amount_text)

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
    monthly_data = {m: float(quantize_money(monthly[m], base_currency)) for m in sorted_months}
    monthly_ranges_data = {m: monthly_ranges[m] for m in sorted_months}

    # Top 5
    top5 = [
        {"name": n, "total": float(quantize_money(t, base_currency))}
        for n, t in merchant_totals.most_common(5)
    ]

    if scope == "cycle":
        burn_rate = _build_cycle_burn_rate(
            today=today,
            period_start=period_start,
            period_end=period_end,
            total_spent=total_spent,
            currency=base_currency,
        )

    insights = _build_summary_insights(
        scope=scope,
        rows=rows,
        period_start=period_start,
        period_end=period_end,
        cycle_rule=cycle_rule,
        total_spent=quantize_money(total_spent, base_currency),
        uncategorized=uncategorized,
        burn_rate=burn_rate,
        budget_limits=budget_limits,
        currency=base_currency,
    )

    return {
        "total_spent": float(quantize_money(total_spent, base_currency)),
        "total_income": float(quantize_money(total_income, base_currency)),
        "subscriptions": float(quantize_money(subscriptions, base_currency)),
        "uncategorized": uncategorized,
        "uncategorized_total": uncategorized_total,
        "by_category": {
            k: float(quantize_money(v, base_currency))
            for k, v in sorted(by_category.items(), key=lambda x: -x[1])
        },
        "monthly": monthly_data,
        "monthly_ranges": monthly_ranges_data,
        "top_merchants": top5,
        "current_cycle": _build_cycle_payload(cycle_rule),
        "scope": scope,
        "scope_label": scope_label,
        "base_currency": base_currency,
        "non_base_currency_count": non_base_currency_count,
        "selected_period": {
            "start": period_start.isoformat(),
            "end": period_end.isoformat(),
            "label": scope_label,
        },
        **preferences,
        "has_data": in_scope_count > 0 or non_base_currency_count > 0,
        "burn_rate": burn_rate,
        "insights": insights,
        "needs_review_count": needs_review_count,
        "confirmed_internal_transfers_count": confirmed_internal_transfers_count,
    }


# ── API: Transactions ────────────────────────────────────────────


@app.get("/api/transactions")
def api_transactions(
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
def api_export_transactions(
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
def api_update_transaction(tx_id: str, body: dict[str, Any]):
    """Update merchant name and/or category for a transaction."""
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
def api_bulk_update_category(body: dict[str, Any]):
    """Apply one category to multiple transactions quickly."""
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
        merchant_rows = db.read_model("transaction_merchants", ids=cleaned_ids)
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
def api_review(
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
def api_export_review(
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
def api_review_transaction(tx_id: str, body: dict[str, Any]):
    """Resolve or update a review-queue item."""
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
def api_confirm_transfer_group(body: dict[str, Any]):
    """Confirm or dismiss a suggested transfer pair."""
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
def api_categories():
    """Return all known categories."""
    with _get_db() as db:
        categories = db.list_known_categories()
    return {"categories": categories}


@app.get("/api/accounts")
def api_accounts():
    """Return accounts and their latest explicitly imported balances."""
    with _get_db() as db:
        return {"accounts": db.list_accounts()}


@app.get("/api/categories/options")
def api_categories_options():
    """Return all categories."""
    with _get_db() as db:
        known_cats = db.list_known_categories()
    other_cats = [row[1] for row in build_seed_data()]
    return {"categories": sorted(set(known_cats + other_cats))}

# ── API: Settings ────────────────────────────────────────────────


@app.get("/api/settings")
def api_settings():
    """Return current config for the settings page."""
    settings = load_settings()
    with _get_db() as db:
        preferences = _load_app_preferences(db)
        effective_currency = _resolve_base_currency(settings, db)
        setup_status = _build_setup_status(settings, db)
        ledger_summary = db.read_model("setup_summary")
        tx_count = ledger_summary["transaction_count"]
        legacy_currency_count = ledger_summary["legacy_currency_count"]
        merchant_count = ledger_summary["merchant_count"]
        feedback_count = ledger_summary["feedback_count"]
        active_rule_count = ledger_summary["active_rule_count"]
        cats = ledger_summary["categories"]
    return {
        "currency": effective_currency,
        "tx_count": tx_count,
        "legacy_currency_count": legacy_currency_count,
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


@app.get("/api/settings/legacy-currency")
def api_legacy_currency_review(
    page: int = Query(1, ge=1),
    per_page: int = Query(50, ge=1, le=200),
):
    """List old transactions whose denomination needs explicit confirmation."""
    with _get_db() as db:
        return db.list_legacy_unknown_currency(page=page, per_page=per_page)


@app.patch("/api/settings/legacy-currency")
def api_confirm_legacy_currency(body: dict[str, Any]):
    """Record user-confirmed currencies for legacy rows without changing amounts."""
    items = body.get("items")
    if not isinstance(items, list) or not all(isinstance(item, dict) for item in items):
        return JSONResponse({"error": "items must be a list of {id, currency} values"}, status_code=400)

    def persist() -> int:
        settings = load_settings()
        with LedgerClient(settings) as db:
            return db.confirm_legacy_currencies(items)

    try:
        confirmed = persist()
    except ValueError as exc:
        return JSONResponse({"error": str(exc)}, status_code=400)
    return {"ok": True, "confirmed": confirmed}


@app.patch("/api/settings/preferences")
def api_update_preferences(body: dict[str, Any]):
    """Save dashboard-local preferences such as theme and cycle start day."""
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
def api_get_category_rules():
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
def api_create_category_rule(body: dict[str, Any]):
    """Create a categorization rule (contains/regex => category)."""
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
def api_update_category_rule(rule_id: int, body: dict[str, Any]):
    """Toggle or reorder a categorization rule."""
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
def api_test_category_rule(body: dict[str, Any]):
    """Preview whether a rule would match sample text and historical rows."""
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
def api_delete_category_rule(rule_id: int):
    """Delete a categorization rule by ID."""
    with _get_db() as db:
        deleted = db.delete_category_rule(rule_id)
    if not deleted:
        return JSONResponse({"error": "Rule not found"}, status_code=404)
    return {"ok": True, "id": rule_id}


@app.get("/api/settings/learning")
def api_learning_summary():
    """Return recent learning events and summary counters."""
    with _get_db() as db:
        events = db.get_recent_learning_feedback(limit=40)
        summary = db.read_model("learning_summary")

    return {
        "events": events,
        "summary": {
            "feedback_count": summary["feedback_count"],
            "override_count": summary["override_count"],
            "uncategorized_count": summary["uncategorized_count"],
            "learned_future_count": summary["learned_future_count"],
        },
    }


@app.post("/api/settings/learning/reapply")
def api_reapply_learning():
    """Re-run deterministic learning on historical transactions."""
    with _get_db() as db:
        result = db.reapply_learning_to_history()
    return {"ok": True, **result}


@app.post("/api/settings/reset-db")
def api_reset_db(body: dict[str, Any]):
    """Reset local SQLite data after explicit confirmation."""
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
    """Validate the upload, then stream events from the independent importer."""
    import json as _json
    from fastapi.responses import StreamingResponse as _SR

    settings = load_settings()
    suffix = Path(file.filename or "upload.csv").suffix.lower()
    if suffix not in {".csv", ".pdf", ".ofx"}:
        return JSONResponse({"error": "Upload one of CSV, PDF, or OFX files."}, status_code=400)
    content = await file.read(settings.max_upload_bytes + 1)
    if len(content) > settings.max_upload_bytes:
        return JSONResponse({"error": "Upload exceeds the configured size limit."}, status_code=413)
    if not content:
        return JSONResponse({"error": "The uploaded file is empty."}, status_code=400)
    if not _UPLOAD_IMPORT_SLOTS.acquire(blocking=False):
        return JSONResponse({"error": "Another import is in progress. Try again shortly."}, status_code=429)

    slot_lock = threading.Lock()
    slot_released = False

    def release_slot() -> None:
        nonlocal slot_released
        with slot_lock:
            if slot_released:
                return
            slot_released = True
        _UPLOAD_IMPORT_SLOTS.release()

    def resolve_currency() -> tuple[str, bool]:
        with LedgerClient(settings) as ledger:
            status = _build_setup_status(settings, ledger)
            return _resolve_base_currency(settings, ledger), bool(status["requires_base_currency_setup"])

    try:
        base_currency, needs_setup = await run_in_threadpool(resolve_currency)
        if needs_setup:
            release_slot()
            return _base_currency_setup_response(include_ok=False)
    except ServiceUnavailable:
        release_slot()
        return JSONResponse({"error": "Ledger service is unavailable. Start the local Spectra services and retry."}, status_code=503)
    except BaseException:
        release_slot()
        raise

    async def events():
        try:
            yield f"data: {_json.dumps({'pct': 5, 'step': 'Upload received'})}\n\n"
            async for event in ImportClient(settings).create_job_stream(
                file.filename or f"statement{suffix}", content, base_currency,
            ):
                yield event
        except ServiceUnavailable:
            yield f"data: {_json.dumps({'pct': 0, 'step': 'Import service is unavailable. Start the local Spectra services and retry.', 'error': True})}\n\n"
        finally:
            release_slot()

    return _SR(events(), media_type="text/event-stream", headers={
        "Cache-Control": "no-cache", "X-Accel-Buffering": "no-cache",
    }, background=BackgroundTask(release_slot))


def _confirm_import_payload(
    settings: Settings,
    payloads: list[dict[str, Any]],
    job_id: str | None = None,
) -> dict[str, Any] | JSONResponse:
    """Delegate review confirmation to the importer, which commits via ledger API."""
    import asyncio

    try:
        with LedgerClient(settings) as ledger:
            setup = _build_setup_status(settings, ledger)
        if setup["requires_base_currency_setup"]:
            return _base_currency_setup_response(include_ok=True)
        return asyncio.run(ImportClient(settings).confirm(job_id, payloads))
    except ServiceUnavailable:
        return JSONResponse(
            {"ok": False, "message": "Import service is unavailable. Retry without changing this review."},
            status_code=503,
        )
    except ServiceResponseError as exc:
        return JSONResponse(
            {
                "ok": False,
                "message": str(exc),
                "retryable": exc.status_code >= 500,
            },
            status_code=exc.status_code if 400 <= exc.status_code < 600 else 502,
        )


@app.post("/api/confirm")
async def api_confirm(request: Request):
    """Confirm and atomically save reviewed transactions off the event loop."""
    import asyncio

    body = await request.json()
    transactions = body.get("transactions", [])
    if not isinstance(transactions, list) or not transactions:
        return {"ok": False, "message": "No transactions to save"}
    if any(not isinstance(item, dict) for item in transactions):
        return JSONResponse({"error": "transactions must be a list of transaction objects"}, status_code=400)

    settings = load_settings()
    job_id = str(body.get("job_id", "") or "").strip() or None
    if job_id:
        return await asyncio.to_thread(_confirm_import_payload, settings, transactions, job_id)
    return await asyncio.to_thread(_confirm_import_payload, settings, transactions)


@app.get("/budget", response_class=HTMLResponse)
def page_budget(request: Request):
    if (redirect := _setup_redirect_if_needed(request)):
        return redirect
    return templates.TemplateResponse(request, "budget.html", _template_context(request))


@app.get("/trends", response_class=HTMLResponse)
def page_trends(request: Request):
    if (redirect := _setup_redirect_if_needed(request)):
        return redirect
    return templates.TemplateResponse(request, "trends.html", _template_context(request))


# ── API: Budget ──────────────────────────────────────────────────


@app.get("/api/budget")
def api_budget():
    """Return per-category budget status for the current month."""
    preferences = _load_app_preferences()
    current_cycle = _build_cycle_payload(preferences["cycle_rule"])

    with _get_db() as db:
        base_currency = _resolve_base_currency(load_settings(), db)
        # All expense rows for the current financial cycle
        rows = db.read_model(
            "budget_expense_rows", start=current_cycle["start"],
            end=current_cycle["end"], currency=base_currency,
        )
        spent_totals: dict[str, Decimal] = {}
        for category, amount_text, legacy_amount in rows:
            spent_totals[category] = spent_totals.get(category, Decimal(0)) + abs(
                as_decimal(amount_text if amount_text is not None else legacy_amount)
            )
        non_base_currency_count = int(db.read_model(
            "currency_count", start=current_cycle["start"], end=current_cycle["end"],
            currency=base_currency,
        ))

        limits = db.get_budget_limits(base_currency)
        exact_limits = db.get_budget_limits_exact(base_currency)
        non_base_budget_count = db.count_non_base_budget_limits(base_currency)

        # All-time categories so we can show unspent ones too
        all_cats = db.read_model("budget_categories", currency=base_currency)

    spent_by_cat: dict[str, Decimal] = spent_totals
    categories = sorted(set(all_cats) | set(limits.keys()))

    items = []
    for cat in categories:
        spent_decimal = quantize_money(spent_by_cat.get(cat, Decimal(0)), base_currency)
        spent = float(spent_decimal)
        limit_decimal = exact_limits.get(cat)
        limit = float(limit_decimal) if limit_decimal is not None else None
        if limit_decimal is not None and limit_decimal > 0:
            pct = round(float(spent_decimal / limit_decimal * 100), 1)
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
        "base_currency": base_currency,
        "non_base_currency_count": non_base_currency_count,
        "non_base_budget_count": non_base_budget_count,
        "items": items,
        "summary": {"on_track": on_track, "over": over, "no_limit": no_limit},
    }


@app.patch("/api/budget/{category}")
def api_update_budget(category: str, body: dict[str, Any]):
    """Save or update a monthly budget limit for a category."""
    limit = body.get("limit")
    try:
        exact_limit = as_decimal(limit)
    except ValueError:
        return JSONResponse({"error": "limit must be a valid monetary amount"}, status_code=400)
    if exact_limit < 0:
        return JSONResponse({"error": "limit must be a non-negative number"}, status_code=400)

    with _get_db() as db:
        currency = _resolve_base_currency(load_settings(), db)
        db.save_budget_limit(category, exact_limit, currency=currency)

    return {
        "ok": True,
        "category": category,
        "currency": currency,
        "limit": float(quantize_money(exact_limit, currency)),
    }


# ── API: Trends ──────────────────────────────────────────────────


@app.get("/api/trends")
def api_trends():
    """Return year-over-year financial data for the Trends page."""
    from collections import defaultdict

    preferences = _load_app_preferences()
    cycle_rule = preferences["cycle_rule"]

    with _get_db() as db:
        base_currency = _resolve_base_currency(load_settings(), db)
        rows = db.read_model("trend_rows", currency=base_currency)
        non_base_currency_count = int(db.read_model(
            "all_time_currency_count", currency=base_currency,
        ))

    if not rows:
        return {
            "years": [], "by_year": {}, "period_series": [],
            "base_currency": base_currency,
            "non_base_currency_count": non_base_currency_count,
            **preferences,
        }

    # Aggregate by year and month
    by_year: dict[str, dict] = {}
    monthly_income: dict[str, Decimal] = defaultdict(Decimal)
    monthly_expense: dict[str, Decimal] = defaultdict(Decimal)
    cat_by_year: dict[str, dict[str, Decimal]] = defaultdict(lambda: defaultdict(Decimal))

    for date_str, amount_text, category in rows:
        amount = as_decimal(amount_text)
        tx_date = parse_iso_date(date_str)
        cycle_start = cycle_start_for(tx_date, cycle_rule)
        year = str(cycle_start.year)
        month = cycle_start.isoformat()

        if year not in by_year:
            by_year[year] = {"income": Decimal(0), "expenses": Decimal(0)}

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
        inc_decimal = quantize_money(by_year[y]["income"], base_currency)
        exp_decimal = quantize_money(by_year[y]["expenses"], base_currency)
        net_decimal = quantize_money(inc_decimal - exp_decimal, base_currency)
        inc = float(inc_decimal)
        exp = float(exp_decimal)
        net = float(net_decimal)
        savings_rate = round(float(net_decimal / inc_decimal * 100), 1) if inc_decimal > 0 else 0.0
        year_stats[y] = {
            "income": inc,
            "expenses": exp,
            "net": net,
            "savings_rate": savings_rate,
            "by_category": {k: float(quantize_money(v, base_currency)) for k, v in sorted(
                cat_by_year[y].items(), key=lambda x: -x[1]
            )[:8]},  # top 8 categories
        }

    # Build monthly series for charts (all years combined, keyed by YYYY-MM)
    all_months = sorted(set(monthly_income) | set(monthly_expense))
    monthly_series = [
        {
            "period_start": m,
            "period_end": next_cycle_start(parse_iso_date(m), cycle_rule).isoformat(),
            "income": float(quantize_money(monthly_income.get(m, Decimal(0)), base_currency)),
            "expenses": float(quantize_money(monthly_expense.get(m, Decimal(0)), base_currency)),
        }
        for m in all_months
    ]

    return {
        "years": years,
        "by_year": year_stats,
        "period_series": monthly_series,
        "base_currency": base_currency,
        "non_base_currency_count": non_base_currency_count,
        **preferences,
    }


@app.get("/api/subscriptions")
def api_subscriptions():
    """Return recurring subscriptions with monthly and annual projections."""
    from collections import defaultdict
    from datetime import date, timedelta
    from statistics import median

    preferences = _load_app_preferences()
    cycle_start, cycle_end = cycle_window_for(date.today(), preferences["cycle_rule"])

    with _get_db() as db:
        base_currency = _resolve_base_currency(load_settings(), db)
        rows = db.read_model("subscription_rows", currency=base_currency)
        non_base_currency_count = int(db.read_model(
            "all_time_currency_count", currency=base_currency,
        ))

    buckets: dict[str, dict[str, Any]] = defaultdict(
        lambda: {
            "name": "",
            "dates": [],
            "amounts": [],
            "in_cycle": Decimal(0),
            "last_date": "",
            "category": "Digital Subscriptions",
        }
    )

    for date_str, clean_name, amount_text, category, original_description in rows:
        amount = as_decimal(amount_text)
        if amount >= 0:
            continue

        recurring_kind = detect_recurring_kind(clean_name, original_description, float(amount))
        if recurring_kind != "Subscription" and category != "Digital Subscriptions":
            continue

        tx_date = parse_iso_date(date_str)
        bucket = buckets[clean_name]
        bucket["name"] = clean_name
        bucket["category"] = category or "Digital Subscriptions"
        bucket["dates"].append(tx_date)
        bucket["amounts"].append(abs(amount))
        if cycle_start <= tx_date < cycle_end:
            bucket["in_cycle"] += abs(amount)
        if date_str > bucket["last_date"]:
            bucket["last_date"] = date_str

    items: list[dict[str, Any]] = []
    monthly_total = Decimal(0)
    price_change_count = 0

    for merchant, bucket in buckets.items():
        dates = sorted(set(bucket["dates"]))
        amounts = bucket["amounts"]
        if not amounts:
            continue

        avg_amount = sum(amounts, Decimal(0)) / Decimal(len(amounts))
        intervals = [
            (dates[i] - dates[i - 1]).days
            for i in range(1, len(dates))
            if (dates[i] - dates[i - 1]).days > 0
        ]
        cadence_days = int(round(median(intervals))) if intervals else 30
        cadence_days = max(cadence_days, 1)

        monthly_estimate = avg_amount * (Decimal("30.4375") / Decimal(cadence_days))
        annual_projection = monthly_estimate * 12
        monthly_total += monthly_estimate

        last_date = parse_iso_date(bucket["last_date"]) if bucket["last_date"] else dates[-1]
        next_charge = last_date + timedelta(days=cadence_days)
        previous_amounts = amounts[:-1]
        baseline_amount = median(previous_amounts) if previous_amounts else amounts[-1]
        change_amount = quantize_money(amounts[-1] - baseline_amount, base_currency)
        change_pct = round(float(change_amount / baseline_amount * 100), 1) if baseline_amount else 0.0
        price_change_direction = ""
        if previous_amounts and abs(change_amount) >= max(Decimal(1), baseline_amount * Decimal("0.08")):
            price_change_direction = "up" if change_amount > 0 else "down"
            price_change_count += 1

        items.append(
            {
                "merchant": merchant,
                "category": bucket["category"],
                "last_amount": float(quantize_money(amounts[-1], base_currency)),
                "average_amount": float(quantize_money(avg_amount, base_currency)),
                "cadence_days": cadence_days,
                "last_charge_date": last_date.isoformat(),
                "next_estimated_date": next_charge.isoformat(),
                "monthly_estimate": float(quantize_money(monthly_estimate, base_currency)),
                "annual_projection": float(quantize_money(annual_projection, base_currency)),
                "in_current_cycle": float(quantize_money(bucket["in_cycle"], base_currency)),
                "payments_count": len(amounts),
                "price_change_direction": price_change_direction,
                "change_amount": float(change_amount),
                "change_pct": change_pct,
            }
        )

    items.sort(key=lambda row: row["monthly_estimate"], reverse=True)
    current_cycle_total = sum(
        (bucket["in_cycle"] for bucket in buckets.values()), Decimal(0),
    )

    return {
        "items": items,
        "base_currency": base_currency,
        "non_base_currency_count": non_base_currency_count,
        "summary": {
            "active_count": len(items),
            "monthly_estimate": float(quantize_money(monthly_total, base_currency)),
            "annual_projection": float(quantize_money(monthly_total * 12, base_currency)),
            "in_current_cycle": float(quantize_money(current_cycle_total, base_currency)),
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
