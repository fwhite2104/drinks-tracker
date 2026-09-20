"""Per-retailer listing finders (R6 partial extraction from collector.py).

Each finder locates the one mapped listing inside a retailer search response
(``LookupError`` when absent) and each extractor pulls a typed money value
(Clubcard price, DRS deposit) from a listing record. These are the clearest
seams of ``collector.py``: pure functions over response payloads, shared
shape, no persistence. The Dunnes finder (``_find_listing``) stays in
``collector.py`` — it handles the three-part VTEX product/item/offer envelope.
"""

from __future__ import annotations

import re
from decimal import ROUND_HALF_UP, Decimal
from typing import TYPE_CHECKING, Any, Mapping

from .money import decimal_price

if TYPE_CHECKING:  # mapping dataclasses live in collector (import cycle avoided)
    from .collector import AldiMapping, LidlMapping, SuperValuMapping, TescoMapping


def _normalise_name(value: str) -> set[str]:
    return set(re.findall(r"[a-z0-9]+", value.lower().replace("-", " ")))


def _name_matches(expected_tokens: set[str], name: str) -> bool:
    """Every expected catalog token appears in the normalised source name."""
    return expected_tokens.issubset(_normalise_name(name))


# Composition parsing: explicit multipack count and unit size in listing names.
# Validated incident (research/wrong-product-audit-2026-09-20.md): a single-can
# cell was observed at a 12-pack price for days because "12 x 330ml" is a
# token superset of "330ml Can" and no layer compared counts.
_COUNT_SIZE_RE = re.compile(
    r"(\d+)\s*[x×]\s*(\d+(?:[.,]\d+)?)", re.IGNORECASE
)
_COUNT_SIZE_UNIT_RE = re.compile(
    r"\s*(ml|cl|litres?|liters?|ltr|l)\b", re.IGNORECASE
)
_XCOUNT_RE = re.compile(r"[x×]\s*(\d+)\b", re.IGNORECASE)
_PACKWORD_RE = re.compile(
    r"(?:\bpack\s+of\s+(\d+)\b)|(?:(\d+)\s*(?:packs?|cans?|bottles?|pouches?)\b)",
    re.IGNORECASE,
)
_SIZE_RE = re.compile(
    r"(\d+(?:[.,]\d+)?)\s*(ml|cl|litres?|liters?|ltr|l)\b", re.IGNORECASE
)


def _parse_composition(name: str) -> tuple[int | None, int | None]:
    """Explicit multipack count and unit size (ml) stated in a listing name.

    ``(None, None)`` when the name states neither: an unstated value is never
    a conflict. Count patterns run in precedence order so "12 x 330ml" reads
    as count 12 (never 330 via the bare ``x`` pattern).
    """
    count: int | None = None
    size_ml: int | None = None
    pair = _COUNT_SIZE_RE.search(name)
    if pair is not None:
        count = int(pair.group(1))
        unit = _COUNT_SIZE_UNIT_RE.match(name, pair.end())
        if unit is not None:
            unit_name = unit.group(1).lower()
            if unit_name not in {"ml", "cl"}:  # l, ltr, litres, liters
                unit_name = "l"
            factor = {"ml": 1, "cl": 10, "l": 1000}[unit_name]
            size_ml = round(float(pair.group(2).replace(",", ".")) * factor)
    else:
        xcount = _XCOUNT_RE.search(name)
        packword = _PACKWORD_RE.search(name)
        if xcount is not None:
            count = int(xcount.group(1))
        elif packword is not None:
            count = int(packword.group(1) or packword.group(2))
    if size_ml is None:
        bare = _SIZE_RE.search(name)
        if bare is not None:
            value = float(bare.group(1).replace(",", "."))
            unit_name = str(bare.group(2)).lower()
            if unit_name == "ml":
                size_ml = round(value)
            elif unit_name == "cl":
                size_ml = round(value * 10)
            else:
                size_ml = round(value * 1000)
    return count, size_ml


def _composition_conflict(
    listing_name: str, expected_count: int, expected_size_ml: int
) -> str | None:
    """Reason string when a listing name's explicit composition contradicts
    the expected pack, else ``None``. Unstated values never conflict."""
    count, size_ml = _parse_composition(listing_name)
    if count is not None and count != expected_count:
        return (
            f"pack count conflict: listing says {count}, pack is {expected_count}"
        )
    if size_ml is not None and size_ml != expected_size_ml:
        return (
            f"unit size conflict: listing says {size_ml}ml, pack is "
            f"{expected_size_ml}ml"
        )
    return None


def _listing_matches(expected_product_name: str, listing_name: str) -> bool:
    """Token match plus composition agreement against the expected name.

    Shared guard for the per-retailer finders' no-source-id fallback: a
    multipack listing must never satisfy a single-pack search (and vice
    versa) merely because its name contains all expected tokens. An expected
    name that states no count is a single; sizes only conflict when both
    sides state one.
    """
    if not _name_matches(_normalise_name(expected_product_name), listing_name):
        return False
    expected_count, expected_size = _parse_composition(expected_product_name)
    listing_count, listing_size = _parse_composition(listing_name)
    if listing_count is not None and listing_count != (expected_count or 1):
        return False
    if expected_size is not None and listing_size is not None and listing_size != expected_size:
        return False
    return True


def _price_value(value: Any) -> Any:
    if isinstance(value, dict):
        for key in ("priceNumeric", "amount", "value", "price"):
            if value.get(key) is not None:
                return value[key]
    return value


def _optional_price(item: Mapping[str, Any], *keys: str) -> Decimal | None:
    for key in keys:
        if item.get(key) is not None:
            return decimal_price(_price_value(item[key]))
    return None


def _find_supervalu_listing(
    payload: Mapping[str, Any], mapping: SuperValuMapping
) -> dict[str, Any]:
    items = payload.get("items")
    if not isinstance(items, list):
        raise ValueError("SuperValu response has no items list")
    for item in items:
        if not isinstance(item, dict):
            continue
        product_id = str(item.get("productId") or item.get("sku") or "")
        if mapping.source_product_id and product_id != str(mapping.source_product_id):
            continue
        if not mapping.source_product_id and not _listing_matches(
            mapping.expected_product_name, str(item.get("name") or "")
        ):
            continue
        return item
    raise LookupError("mapped SuperValu product was not found")


def _supervalu_drs_deposit(item: Mapping[str, Any]) -> Decimal | None:
    tax_details = item.get("taxDetails") or []
    if isinstance(tax_details, list):
        for detail in tax_details:
            if isinstance(detail, dict) and str(detail.get("groupName", "")).lower() == "deposit":
                return decimal_price(detail.get("amount"))
    return _optional_price(item, "drsDeposit", "deposit", "depositAmount")


def _find_tesco_listing(
    payload: Mapping[str, Any], mapping: TescoMapping
) -> dict[str, Any]:
    products = payload.get("products")
    if not isinstance(products, list):
        raise ValueError("Tesco response has no products list")
    for product in products:
        if not isinstance(product, dict):
            continue
        tpnb = str(product.get("tpnb") or "")
        if mapping.source_tpnb and tpnb != str(mapping.source_tpnb):
            continue
        if not mapping.source_tpnb and not _listing_matches(
            mapping.expected_product_name, str(product.get("title") or "")
        ):
            continue
        return product
    raise LookupError("mapped Tesco product was not found")


def _tesco_clubcard_price(item: Mapping[str, Any]) -> Decimal | None:
    """Clubcard (loyalty) price for the exact pack, if the listing carries one.

    Precedence: an explicit loyalty price field, then promotion descriptions.
    Validated against live responses (cross-checked with the working Monster
    tracker extraction): Tesco tags Clubcard promotions with a
    ``CLUBCARD_PRICING`` attribute and phrases them as "€X Clubcard Price" or
    "Any N for €X Clubcard Price". The multi-buy form records the effective
    per-pack price X/N — what a loyalty member actually pays per pack.
    Promotions without the attribute keep the conservative treatment:
    unattributed "N for €X" text is ordinary multi-buy marketing, not a
    loyalty price, and meal-deal phrasing never prices this pack.
    """
    price = _optional_price(item, "clubcardPrice", "clubCardPrice", "loyaltyPrice")
    if price is not None:
        return price
    for promotion in item.get("promotions") or []:
        if not isinstance(promotion, dict):
            continue
        description = str(promotion.get("description", ""))
        lowered = description.lower()
        if "meal deal" in lowered:
            continue
        attributes = promotion.get("attributes")
        attributes = attributes if isinstance(attributes, list) else []
        is_clubcard_promotion = any(
            "CLUBCARD" in str(attribute).upper() for attribute in attributes
        )
        multi = re.search(
            r"(?:any\s+)?(\d+)\s+for\s+€\s*([0-9]+(?:[.,][0-9]+)?)",
            description,
            re.IGNORECASE,
        )
        if multi and is_clubcard_promotion:
            quantity = Decimal(multi.group(1))
            total = decimal_price(multi.group(2).replace(",", "."))
            if quantity > 0 and total is not None:
                return (total / quantity).quantize(Decimal("0.01"), ROUND_HALF_UP)
            continue
        if multi:
            continue
        if re.search(r"club\s*card\s*price", description, re.IGNORECASE):
            match = re.search(r"€\s*([0-9]+(?:[.,][0-9]+)?)", description)
            if match:
                return decimal_price(match.group(1).replace(",", "."))
    return None


def _tesco_drs_deposit(item: Mapping[str, Any]) -> Decimal | None:
    """Deposit Return Scheme charge for the pack, if the listing carries one.

    Highest precedence is the structured ``charges`` fragment
    (``ProductDepositReturnCharge``), validated against live responses —
    Tesco's GraphQL serves the deposit there; the ``details.taxDetails``
    group fallback predates it and is kept for robustness.
    """
    charges = item.get("charges")
    if isinstance(charges, list):
        for charge in charges:
            if isinstance(charge, dict) and charge.get("amount") is not None:
                amount = decimal_price(charge["amount"])
                if amount is not None:
                    return amount
    details = item.get("details")
    if isinstance(details, dict):
        tax_details = details.get("taxDetails") or details.get("taxes") or []
        if isinstance(tax_details, list):
            for detail in tax_details:
                if isinstance(detail, dict) and str(detail.get("groupName", "")).lower() == "deposit":
                    return decimal_price(detail.get("amount"))
        return _optional_price(details, "drsDeposit", "deposit", "depositAmount")
    return _optional_price(item, "drsDeposit", "deposit", "depositAmount")


def _find_lidl_listing(
    payload: Mapping[str, Any], mapping: LidlMapping
) -> dict[str, Any]:
    items = payload.get("items")
    if not isinstance(items, list):
        raise ValueError("Lidl response has no items list")
    for item in items:
        if not isinstance(item, dict):
            continue
        product_id = str(item.get("productId") or "")
        if mapping.source_product_id and product_id != str(mapping.source_product_id):
            continue
        if not mapping.source_product_id and not _listing_matches(
            mapping.expected_product_name, str(item.get("name") or "")
        ):
            continue
        return item
    raise LookupError("mapped Lidl product was not found")


def _lidl_drs_deposit(item: Mapping[str, Any]) -> Decimal | None:
    """Extract the Lidl refundable deposit from price evidence.

    Verified evidence shape: the current price block carries the deposit in
    ``basePrice.text`` (for example ``"€2.25 Deposit Return"``) in place of
    the usual unit-price text.  ``specialTaxes`` entries naming a deposit
    are honoured first, but that list has not been observed populated for
    Lidl Ireland, so its entry shape is unverified.
    """
    for tax in item.get("specialTaxes") or []:
        if not isinstance(tax, Mapping):
            continue
        label = " ".join(
            str(tax.get(key) or "")
            for key in ("label", "name", "type", "group", "groupName")
        )
        if "deposit" not in label.lower():
            continue
        amount = tax.get("amount")
        if amount is None:
            amount = tax.get("value")
        if amount is not None:
            return decimal_price(amount)
    text = item.get("basePriceText")
    if text is not None and re.search(r"deposit", str(text), re.IGNORECASE):
        match = re.search(r"€\s*([0-9]+(?:[.,][0-9]+)?)", str(text))
        if match:
            return decimal_price(match.group(1).replace(",", "."))
    return None


def _find_aldi_listing(
    payload: Mapping[str, Any], mapping: AldiMapping
) -> dict[str, Any]:
    items = payload.get("items")
    if not isinstance(items, list):
        raise ValueError("Aldi response has no items list")
    for item in items:
        if not isinstance(item, dict):
            continue
        product_id = str(item.get("productId") or "")
        if mapping.source_product_id and product_id != str(mapping.source_product_id):
            continue
        if not mapping.source_product_id and not _listing_matches(
            mapping.expected_product_name, str(item.get("name") or "")
        ):
            continue
        return item
    raise LookupError("mapped Aldi product was not found")


def _aldi_drs_deposit(item: Mapping[str, Any]) -> Decimal | None:
    """Extract the Aldi refundable deposit from price evidence.

    The source exposes a structured ``price.bottleDeposit`` (integer cents)
    with a euro display string.  Verified limitation: every captured Aldi IE
    online listing reported a zero deposit — DRS-eligible cans and small PET
    bottles are not observable in the online range — so only the display-
    string path is validated against live data.
    """
    text = item.get("bottleDepositText")
    if text is None:
        return None
    value = decimal_price(text)
    # A €0.00 deposit display means "no deposit evidence": persisting it
    # would invent a DRS Deposit where the source states none.
    if value == Decimal("0"):
        return None
    return value
