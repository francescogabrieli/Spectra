"""Validated foreign-exchange conversions with explicit failure semantics."""

from __future__ import annotations

import logging
import math
import re
import threading
from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from typing import Any, Protocol

from spectra.money import as_decimal, quantize_money

logger = logging.getLogger("spectra.fx")

_CURRENCY_RE = re.compile(r"^[A-Z]{3}$")
_RATE_CACHE: dict[tuple[str, str, str], tuple[Decimal, str, str]] = {}
_RATE_CACHE_LOCK = threading.RLock()


def clear_fx_rate_cache() -> None:
    """Clear process-local rate entries (useful after cache policy changes)."""
    with _RATE_CACHE_LOCK:
        _RATE_CACHE.clear()


class FXRateStore(Protocol):
    def get_fx_rate(self, rate_date: str, base: str, quote: str) -> dict[str, Any] | None: ...
    def save_fx_rate(
        self, rate_date: str, base: str, quote: str, rate: str,
        effective_date: str, provider: str,
    ) -> None: ...


class FXConversionError(ValueError):
    """The requested historical exchange rate could not be established."""


@dataclass(frozen=True)
class ConversionResult:
    amount: Decimal
    rate: Decimal
    requested_date: str
    effective_date: str
    source: str


def _validate_code(value: str) -> str:
    code = str(value or "").strip().upper()
    if not _CURRENCY_RE.fullmatch(code):
        raise ValueError(f"Invalid ISO currency code: {value!r}")
    return code


def _validate_date(value: str) -> str:
    candidate = str(value or "latest").strip()
    if candidate.lower() == "latest":
        return "latest"
    try:
        return date.fromisoformat(candidate).isoformat()
    except ValueError as exc:
        raise ValueError(f"Invalid FX date: {value!r}") from exc


def _read_cached_rate(
    key: tuple[str, str, str], store: FXRateStore | None,
) -> tuple[Decimal, str, str] | None:
    with _RATE_CACHE_LOCK:
        memory_value = _RATE_CACHE.get(key)
        if memory_value:
            return memory_value[0], memory_value[1], "memory-cache"
        if store is None:
            return None
        cached = store.get_fx_rate(*key)
        if not cached:
            return None
        try:
            rate = as_decimal(cached["rate"])
            if rate <= 0:
                raise ValueError("rate must be positive")
            effective_date = str(cached.get("effective_date") or key[0])
            if effective_date != "latest":
                date.fromisoformat(effective_date)
            provider = str(cached.get("provider") or "sqlite-cache")
        except (KeyError, ValueError, TypeError) as exc:
            raise FXConversionError("Stored FX rate is invalid") from exc
        _RATE_CACHE[key] = (rate, effective_date, provider)
        return rate, effective_date, "sqlite-cache"


def _fetch_rate(base: str, quote: str, requested_date: str) -> tuple[Decimal, str]:
    try:
        import httpx

        url = f"https://api.frankfurter.dev/v1/{requested_date}"
        with httpx.Client(timeout=8.0) as client:
            response = client.get(url, params={"base": base, "symbols": quote})
            response.raise_for_status()
            payload = response.json()
    except Exception as exc:
        raise FXConversionError(f"FX service unavailable for {base}/{quote} on {requested_date}") from exc

    if not isinstance(payload, dict) or str(payload.get("base", "")).upper() != base:
        raise FXConversionError("FX provider returned an invalid base currency")
    rates = payload.get("rates")
    if not isinstance(rates, dict) or quote not in rates:
        raise FXConversionError(f"FX provider returned no rate for {quote}")
    try:
        rate = as_decimal(rates[quote])
        numeric_rate = float(rate)
    except (ValueError, OverflowError) as exc:
        raise FXConversionError("FX provider returned an invalid rate") from exc
    if not math.isfinite(numeric_rate) or rate <= 0:
        raise FXConversionError("FX provider returned a non-positive or non-finite rate")
    effective_date = str(payload.get("date") or requested_date)
    if effective_date != "latest":
        try:
            date.fromisoformat(effective_date)
        except ValueError as exc:
            raise FXConversionError("FX provider returned an invalid rate date") from exc
    return rate, effective_date


def convert_currency_result(
    amount: object,
    from_currency: str,
    to_currency: str = "EUR",
    rate_date: str = "latest",
    *,
    rate_store: FXRateStore | None = None,
) -> ConversionResult:
    """Convert exactly, with a currency-aware rounding step and rate provenance.

    The cache is scoped by date, source, and target. Failures raise instead of
    returning an amount that callers could accidentally relabel.
    """
    source = _validate_code(from_currency)
    target = _validate_code(to_currency)
    requested_date = _validate_date(rate_date)
    value = as_decimal(amount)
    if source == target:
        return ConversionResult(
            quantize_money(value, target), Decimal("1"), requested_date,
            requested_date, "identity",
        )

    key = (requested_date, source, target)
    with _RATE_CACHE_LOCK:
        cached = _read_cached_rate(key, rate_store)
        if cached:
            rate, effective_date, provider = cached
            return ConversionResult(
                quantize_money(value * rate, target), rate, requested_date,
                effective_date, provider,
            )

        # The lock covers a cache miss as well as the network call so parallel
        # imports cannot issue duplicate requests for the same historical rate.
        rate, effective_date = _fetch_rate(source, target, requested_date)
        _RATE_CACHE[key] = (rate, effective_date, "frankfurter")
        if rate_store is not None and requested_date != "latest":
            rate_store.save_fx_rate(
                requested_date, source, target, str(rate), effective_date, "frankfurter",
            )
    logger.info("Loaded FX rate for %s/%s on %s (%s)", source, target, effective_date, "Frankfurter")
    return ConversionResult(
        quantize_money(value * rate, target), rate, requested_date,
        effective_date, "frankfurter",
    )


def convert_currency(
    amount: object,
    from_currency: str,
    to_currency: str = "EUR",
    date: str = "latest",
    *,
    rate_store: FXRateStore | None = None,
) -> float:
    """Compatibility wrapper returning a float, raising on unavailable FX."""
    return float(
        convert_currency_result(
            amount, from_currency, to_currency, date, rate_store=rate_store,
        ).amount
    )
