"""Shared money serialization for source price evidence.

CONTRIBUTING §4: prices are ``Decimal`` and serialized through one shared
serializer — ``decimal_text`` (quantize with ``ROUND_HALF_UP``, plain
fixed-point formatting) — never ``float``.  ``euro_display`` renders one
``Decimal`` amount as the euro display string (``"€X.XX"``) that retailer
sources keep in their normalized records.
"""

from __future__ import annotations

from decimal import ROUND_HALF_UP, Decimal, InvalidOperation
from typing import Any


def decimal_text(value: Decimal, places: str = "0.01") -> str:
    """Serialize one ``Decimal`` amount as plain fixed-point text.

    Rounding is ``ROUND_HALF_UP`` at ``places`` decimal places (default
    ``"0.01"``); pass ``"0.0001"`` for derived per-litre values.
    """
    return format(value.quantize(Decimal(places), rounding=ROUND_HALF_UP), "f")


def euro_display(value: Decimal) -> str:
    """Render one ``Decimal`` amount as a euro display string (``"€X.XX"``)."""
    return "€" + decimal_text(value)


def decimal_price(value: Any) -> Decimal:
    """Parse one source price (number or ``"€1,234.56"``-style text) to Decimal.

    Raises ``ValueError`` for missing, malformed, or negative values; callers
    translate that into ``source_error`` per CONTRIBUTING §4/§8.
    """
    if value is None:
        raise ValueError("source response has no price")
    text = str(value).strip().replace("€", "").replace(" ", "")
    if "," in text and "." in text:
        if text.rfind(",") > text.rfind("."):
            text = text.replace(".", "").replace(",", ".")
        else:
            text = text.replace(",", "")
    elif text.count(",") == 1:
        text = text.replace(",", ".")
    else:
        text = text.replace(",", "")
    try:
        price = Decimal(text).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
    except InvalidOperation as exc:
        raise ValueError(f"Invalid source price: {value!r}") from exc
    if price < 0:
        raise ValueError(f"Invalid negative source price: {value!r}")
    return price
