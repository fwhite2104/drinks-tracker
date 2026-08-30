"""Shared money serialization for source price evidence.

CONTRIBUTING §4: prices are ``Decimal`` and serialized through one shared
serializer — ``decimal_text`` (quantize with ``ROUND_HALF_UP``, plain
fixed-point formatting) — never ``float``.  ``euro_display`` renders one
``Decimal`` amount as the euro display string (``"€X.XX"``) that retailer
sources keep in their normalized records.
"""

from __future__ import annotations

from decimal import ROUND_HALF_UP, Decimal


def decimal_text(value: Decimal, places: str = "0.01") -> str:
    """Serialize one ``Decimal`` amount as plain fixed-point text.

    Rounding is ``ROUND_HALF_UP`` at ``places`` decimal places (default
    ``"0.01"``); pass ``"0.0001"`` for derived per-litre values.
    """
    return format(value.quantize(Decimal(places), rounding=ROUND_HALF_UP), "f")


def euro_display(value: Decimal) -> str:
    """Render one ``Decimal`` amount as a euro display string (``"€X.XX"``)."""
    return "€" + decimal_text(value)
