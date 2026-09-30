"""Money handling: integer minor units, no floats on the money path.

All amounts are stored and computed as integer minor units (e.g. cents).
Human-facing decimal inputs are converted via decimal string parsing so that
binary-float artifacts (0.1 + 0.2) can never corrupt a balance.
"""
from __future__ import annotations

from decimal import ROUND_HALF_UP, Decimal

# ISO-4217 minor-unit counts for currencies used by the service.
MINOR_UNITS: dict[str, int] = {
    "USD": 2,
    "EUR": 2,
    "CNY": 2,
    "JPY": 0,
}


def validate_currency(currency: str) -> str:
    """Return the normalized currency code or raise ValueError if unsupported."""
    code = (currency or "").strip().upper()
    if code not in MINOR_UNITS:
        raise ValueError(f"unsupported currency: {currency!r}")
    return code


def to_minor(amount: float | int | str | Decimal, currency: str) -> int:
    """Convert a human decimal amount to integer minor units.

    Conversion goes through ``str``/``Decimal`` with ROUND_HALF_UP, so float
    inputs like 0.1 are parsed as their decimal meaning, not their binary
    representation.
    """
    code = validate_currency(currency)
    dec = Decimal(str(amount))
    if dec.is_nan() or dec.is_infinite():
        raise ValueError("amount must be a finite number")
    digits = MINOR_UNITS[code]
    quantum = Decimal(1).scaleb(-digits)
    minor = int((dec / quantum).to_integral_value(rounding=ROUND_HALF_UP))
    return minor


def from_minor(minor: int, currency: str) -> Decimal:
    """Convert integer minor units back to a Decimal human amount."""
    code = validate_currency(currency)
    digits = MINOR_UNITS[code]
    return Decimal(minor).scaleb(-digits)


def format_money(minor: int, currency: str) -> str:
    """Format minor units as a fixed-point decimal string, e.g. '10.00'."""
    code = validate_currency(currency)
    digits = MINOR_UNITS[code]
    sign = "-" if minor < 0 else ""
    absolute = abs(minor)
    if digits == 0:
        return f"{sign}{absolute}"
    whole, part = divmod(absolute, 10**digits)
    return f"{sign}{whole}.{part:0{digits}d}"
