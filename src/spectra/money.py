"""Exact money helpers used at parser, conversion, and SQLite boundaries."""

from __future__ import annotations

from decimal import Decimal, InvalidOperation, ROUND_HALF_UP

# ISO 4217 currencies with non-two-digit minor units. Unknown codes use the
# common two-digit convention; callers still persist the currency alongside it.
_ZERO_MINOR_UNIT = {"BIF", "CLP", "DJF", "GNF", "ISK", "JPY", "KMF", "KRW", "PYG", "RWF", "UGX", "UYI", "VND", "VUV", "XAF", "XOF", "XPF"}
_THREE_MINOR_UNIT = {"BHD", "IQD", "JOD", "KWD", "LYD", "OMR", "TND"}


def currency_exponent(currency: str | None) -> int:
    code = str(currency or "").strip().upper()
    if code in _ZERO_MINOR_UNIT:
        return 0
    if code in _THREE_MINOR_UNIT:
        return 3
    return 2


def as_decimal(value: object) -> Decimal:
    """Convert user or stored numeric data without importing binary float noise."""
    try:
        amount = Decimal(str(value))
    except (InvalidOperation, ValueError, TypeError) as exc:
        raise ValueError(f"Invalid monetary amount: {value!r}") from exc
    if not amount.is_finite():
        raise ValueError("Monetary amounts must be finite")
    return amount


def quantize_money(value: object, currency: str) -> Decimal:
    amount = as_decimal(value)
    quantum = Decimal(1).scaleb(-currency_exponent(currency))
    return amount.quantize(quantum, rounding=ROUND_HALF_UP)


def decimal_text(value: object, currency: str) -> str:
    """Return a fixed-width decimal suitable as the authoritative SQLite value."""
    amount = quantize_money(value, currency)
    return f"{amount:.{currency_exponent(currency)}f}"


def minor_units(value: object, currency: str) -> int:
    exponent = currency_exponent(currency)
    amount = quantize_money(value, currency)
    return int(amount.scaleb(exponent))


def from_minor_units(value: int, currency: str) -> Decimal:
    return Decimal(int(value)).scaleb(-currency_exponent(currency))
