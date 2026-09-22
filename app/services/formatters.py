"""Centralized number, currency, count and weight formatting utilities.

All user-facing numbers go through these helpers so the rest of the app
cannot drift or double-interpret values. The helpers preserve the underlying
numeric value; they only change its string representation.
"""
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
from typing import Any, Optional, Union


def _indian_grouping(whole: str) -> str:
    """Apply Indian 3-2-2-... grouping to a digit string."""
    if len(whole) <= 3:
        return whole
    last3 = whole[-3:]
    rest = whole[:-3]
    parts = []
    while len(rest) > 2:
        parts.insert(0, rest[-2:])
        rest = rest[:-2]
    if rest:
        parts.insert(0, rest)
    return ",".join(parts) + "," + last3


def format_number(value: Union[int, float, str, None], decimals: int = 0) -> str:
    """Format a generic number with Indian grouping and optional decimals."""
    if value is None:
        return "0"
    try:
        d = Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError):
        return str(value)
    rounded = d.quantize(Decimal("1." + "0" * decimals), rounding=ROUND_HALF_UP)
    sign, digits, _ = rounded.as_tuple()
    whole = "".join(str(d) for d in digits)
    if decimals > 0:
        whole_part = whole[:-decimals] if len(whole) > decimals else "0"
        frac = whole[-decimals:] if len(whole) >= decimals else whole.zfill(decimals)
        return ("-" if sign else "") + _indian_grouping(whole_part) + "." + frac
    return ("-" if sign else "") + _indian_grouping(whole)


def format_count(value: Union[int, float, str, None], unit: str = "") -> str:
    """Format a count (no decimals, Indian grouping) with optional unit suffix."""
    if value is None:
        return "0"
    try:
        num = int(round(float(value)))
    except (TypeError, ValueError):
        return str(value)
    formatted = format_number(num, decimals=0)
    return f"{formatted} {unit}" if unit else formatted


def format_currency(
    value: Union[int, float, str, None],
    currency: str = "INR",
    decimals: int = 2,
    normalize: bool = True,
) -> str:
    """Format a currency amount with Indian notation.

    Large values are normalized to lakh/crore only when ``normalize`` is True.
    The normalized base is still formatted with Indian number grouping.
    """
    if value is None:
        value = 0
    try:
        d = Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError):
        return str(value)

    sign = "-" if d < 0 else ""
    abs_d = abs(d)
    abs_val = float(abs_d)

    if normalize:
        # Use lakh/crore scales that are natural for Indian business users.
        # Always prefer "crore" with Indian grouping for large values rather
        # than inventing "thousand crore" / "lakh crore" tiers that read oddly.
        if abs_val >= 1e7:
            in_crore = abs_d / Decimal("10000000")
            return f"{sign}₹{format_number(float(in_crore), decimals)} crore"
        elif abs_val >= 1e5:
            in_lakh = abs_d / Decimal("100000")
            return f"{sign}₹{format_number(float(in_lakh), decimals)} lakh"
        elif abs_val >= 1e3:
            return f"{sign}₹{format_number(abs_val, 0)}"
        return f"{sign}₹{format_number(abs_val, decimals)}"

    # No normalization: just put the rupee symbol on the full number
    return f"{sign}₹{format_number(abs_val, decimals)}"


def format_currency_dual(
    value: Union[int, float, str, None],
    currency: str = "INR",
    decimals: int = 2,
) -> str:
    """Format a currency value — normalized form only (no duplicate raw amount).

    For large values (>= 1 lakh), returns "₹X.XX crore" or "₹X.XX lakh".
    For smaller values, returns the standard format_currency output.
    """
    return format_currency(value, currency, decimals, normalize=True)


def format_weight(value: Union[int, float, str, None], unit: str = "gms") -> str:
    """Format a weight value with a jewelry-native unit suffix (3 decimals)."""
    if value is None:
        value = 0
    try:
        v = float(value)
    except (TypeError, ValueError):
        return str(value)
    return f"{format_number(v, 3)} {normalize_unit_label(unit)}"


def format_percentage(value: Union[int, float, str, None], decimals: int = 2) -> str:
    """Format a percentage."""
    if value is None:
        return "0%"
    try:
        v = float(value)
    except (TypeError, ValueError):
        return str(value)
    return f"{format_number(v, decimals)}%"


# ── Jewelry-industry unit label normalization ─────────────────────────────────
# Maps internal unit codes (from report_columns.json `unit` field) to the
# short, industry-standard suffixes used in headers and value displays.
# Indian jewellery conventions: g (grams), ct / ctw (carat), pcs (pieces),
# % for tunch/wastage, ₹ for currency.
JEWELRY_UNIT_LABELS: dict = {
    "gms": "gms",
    "gm": "gms",
    "g": "gms",
    "kg": "kg",
    "ctw": "ctw",
    "ct": "ctw",
    "carat": "ctw",
    "pcs": "pcs",
    "pc": "pcs",
    "pieces": "pcs",
    "%": "%",
    "per_gm": "/gms",
    "per_ct": "/ctw",
    "per_gram": "/gms",
    "per_carat": "/ctw",
    "rate": "/gms",
}


def normalize_unit_label(unit: str) -> str:
    """Normalize an internal unit code to its jewelry-industry display form.

    Examples: 'gms' -> 'gms', 'ctw' -> 'ctw', 'pcs' -> 'pcs', '' -> ''.
    Unknown units pass through unchanged.
    """
    if not unit:
        return ""
    return JEWELRY_UNIT_LABELS.get(unit.lower(), unit)
