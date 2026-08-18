"""Retailer discovery seams and source-to-catalog evidence normalization."""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation
from typing import Any, Callable, Mapping, Sequence

from .collector import BenchmarkPack


Completeness = bool | str
_REQUEST_KINDS = {"bootstrap", "search", "pagination", "hydration"}
_ATTRIBUTES = (
    "brand", "variant", "unit_size_ml", "total_volume_ml", "pack_count", "package_type"
)


@dataclass(frozen=True)
class RequestEvent:
    """One source request, including the number of records in a batch."""

    kind: str
    batch_size: int = 1
    metadata: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if self.kind not in _REQUEST_KINDS:
            raise ValueError(f"unsupported discovery request kind: {self.kind}")
        if self.batch_size < 1:
            raise ValueError("request batch size must be positive")


@dataclass(frozen=True)
class PriceEvidence:
    raw_value: Any = None
    status: str = "missing"
    reason: str | None = None


@dataclass(frozen=True)
class NormalizedListing:
    retailer: str
    source_identity: str
    identity_tier: str
    name: str
    canonical_name: str
    raw_record: Any
    raw_attributes: Mapping[str, Any]
    name_attributes: Mapping[str, Any]
    attributes: Mapping[str, Any]
    inference_basis: Mapping[str, str]
    conflicts: Mapping[str, Mapping[str, Any]]
    missing_attributes: tuple[str, ...]
    price: PriceEvidence



@dataclass(frozen=True)
class DiscoveryResult:
    listings: tuple[NormalizedListing, ...]
    complete: Completeness
    pagination: Mapping[str, Any]
    raw_records: tuple[Any, ...]
    request_events: tuple[RequestEvent, ...]

    @property
    def request_counts(self) -> dict[str, int]:
        counts = {kind: 0 for kind in sorted(_REQUEST_KINDS)}
        for event in self.request_events:
            counts[event.kind] += 1
        return counts

    @property
    def batch_sizes(self) -> dict[str, list[int]]:
        result: dict[str, list[int]] = {kind: [] for kind in sorted(_REQUEST_KINDS)}
        for event in self.request_events:
            result[event.kind].append(event.batch_size)
        return result


@dataclass(frozen=True)
class Capability:
    identity_tier: str
    supported: bool
    method: str
    reason: str


@dataclass(frozen=True)
class CapabilityContract:
    capabilities: Mapping[str, Capability]

    def for_tier(self, identity_tier: str) -> Capability:
        return self.capabilities.get(
            identity_tier,
            Capability(identity_tier, False, "none", "no tested path"),
        )

    def supports(self, identity_tier: str) -> bool:
        return self.for_tier(identity_tier).supported


_UNIT_RE = re.compile(r"(?P<value>\d+(?:[.,]\d+)?)\s*(?P<unit>ml|cl|l)\b", re.I)
_COUNT_X_RE = re.compile(r"\b(?P<count>\d+)\s*[x×]\s*(?:\d+(?:[.,]\d+)?\s*(?:ml|cl|l))", re.I)
_COUNT_WORD_RE = re.compile(r"\b(?P<count>\d+)\s*(?:pack|pk|cans?|bottles?|cartons?)\b", re.I)
_TRAILING_X_RE = re.compile(r"[x×]\s*(?P<count>\d+)\b", re.I)


def _text(value: Any) -> str:
    return "" if value is None else str(value).strip()


def _canonical_text(value: Any, aliases: Mapping[str, str] | None = None) -> str:
    text = _text(value).lower().replace("-", " ")
    for source, target in (aliases or {}).items():
        text = re.sub(rf"\b{re.escape(source.lower())}\b", target.lower(), text)
    return " ".join(re.findall(r"[a-z0-9]+", text))


def _apply_aliases(value: Any, aliases: Mapping[str, str] | None = None) -> str:
    text = _text(value).lower().replace("-", " ")
    for source, target in (aliases or {}).items():
        text = re.sub(rf"\b{re.escape(source.lower())}\b", target.lower(), text)
    return " ".join(text.split())


def _number(value: Any) -> float | None:
    try:
        return float(str(value).replace(",", "."))
    except (TypeError, ValueError):
        return None


def _size_ml(value: Any) -> int | None:
    if isinstance(value, Mapping):
        units = _text(value.get("units") or value.get("unit"))
        value = value.get("value")
    else:
        units = ""
    if value is None:
        return None
    match = _UNIT_RE.search(_text(value))
    if match:
        number = _number(match.group("value"))
        units = match.group("unit")
    else:
        number = _number(value)
    if number is None:
        return None
    unit = units.lower()
    multiplier = {"l": 1000, "cl": 10, "ml": 1, "": 1}.get(unit)
    return round(number * multiplier) if multiplier is not None else None


def _name_attributes(name: str) -> dict[str, Any]:
    count_match = _COUNT_X_RE.search(name) or _COUNT_WORD_RE.search(name)
    count = int(count_match.group("count")) if count_match else None
    if count is None:
        trailing = _TRAILING_X_RE.search(name)
        count = int(trailing.group("count")) if trailing else 1
    sizes = list(_UNIT_RE.finditer(name))
    unit_size = _size_ml(sizes[-1].group(0)) if sizes else None
    package_tokens = set(_canonical_text(name).split())
    package_type = (
        "can" if package_tokens & {"can", "cans"} else
        "bottle" if package_tokens & {"bottle", "bottles"} else
        "carton" if package_tokens & {"carton", "cartons"} else None
    )
    return {
        "unit_size_ml": unit_size,
        "pack_count": count,
        "total_volume_ml": unit_size * count if unit_size is not None else None,
        "package_type": package_type,
    }


def _first(record: Mapping[str, Any], *keys: str) -> Any:
    for key in keys:
        value = record.get(key)
        if value is not None and value != "":
            return value
    return None


def _structured_attributes(record: Mapping[str, Any]) -> tuple[dict[str, Any], dict[str, Any]]:
    # Fixture case: Tesco puts packSize and tax metadata below details.
    details = record.get("details")
    source = dict(details) if isinstance(details, Mapping) else {}
    source.update(record)
    raw: dict[str, Any] = {}
    values: dict[str, Any] = {}
    brand = _first(source, "brand", "brandName", "manufacturer", "productBrand")
    variant = _first(source, "variant", "flavour", "flavor", "productVariant")
    if brand is not None:
        raw["brand"] = brand
        values["brand"] = _text(brand)
    if variant is not None:
        raw["variant"] = variant
        values["variant"] = _text(variant)

    count = _first(source, "packCount", "pack_count", "quantity", "itemsPerPack")
    count = int(count) if str(count).isdigit() else None
    pack_size_hint = _first(source, "packSize", "size", "volume")
    if count is None and pack_size_hint is not None:
        count_match = _COUNT_X_RE.search(_text(pack_size_hint)) or _COUNT_WORD_RE.search(_text(pack_size_hint))
        count = int(count_match.group("count")) if count_match else None
    if count:
        raw["pack_count"] = count
        values["pack_count"] = count

    unit_value = _first(source, "unitSizeMl", "unit_size_ml", "unitSize", "unitVolume")
    total_value = _first(source, "totalVolumeMl", "total_volume_ml", "totalVolume")
    pack_size = pack_size_hint
    if unit_value is None and pack_size is not None:
        unit_value = pack_size
    if unit_value is not None:
        raw["unit_size"] = unit_value
        parsed = _size_ml(unit_value)
        if parsed is not None:
            values["unit_size_ml"] = parsed
    if total_value is not None:
        raw["total_volume"] = total_value
        parsed = _size_ml(total_value)
        if parsed is not None:
            values["total_volume_ml"] = parsed
    package = _first(source, "packageType", "package_type", "container", "format")
    if package is not None:
        raw["package_type"] = package
        package_text = _canonical_text(package)
        if "can" in package_text:
            values["package_type"] = "can"
        elif "bottle" in package_text:
            values["package_type"] = "bottle"
        elif "carton" in package_text:
            values["package_type"] = "carton"
    return raw, values


def _identity(retailer: str, record: Mapping[str, Any], name: str, attributes: Mapping[str, Any]) -> tuple[str, str]:
    if retailer == "dunnes":
        reference = _text(_first(record, "productReference", "product_reference"))
        item = _text(_first(record, "itemId", "item_id"))
        if reference and item:
            return f"{reference}:{item}", "composite"
        if item:
            return item, "item"
        if reference:
            return reference, "product"
    elif retailer == "supervalu":
        product = _text(_first(record, "productId", "product_id", "sku"))
        if product:
            return product, "product"
    elif retailer == "tesco":
        tpnb = _text(_first(record, "tpnb", "TPNB"))
        if tpnb:
            return tpnb, "tpnb"
    signature = "|".join(
        _text(attributes.get(key)) for key in
        ("brand", "variant", "unit_size_ml", "pack_count", "package_type")
    )
    return f"{_canonical_text(name)}|{signature}", "name_pack_signature"


def _price(record: Mapping[str, Any]) -> PriceEvidence:
    value = _first(record, "priceNumeric", "price", "actual", "displayedPrice", "displayed_price")
    promotions = _first(record, "promotion", "promotions")
    if value is None:
        if promotions:
            return PriceEvidence(None, "unsupported_promotion", "promotion has no standalone price")
        return PriceEvidence(None, "missing")
    if isinstance(value, Mapping):
        raw = _first(value, "actual", "amount", "value")
        if raw is None:
            if promotions:
                return PriceEvidence(value, "unsupported_promotion", "promotion has no standalone price")
            return PriceEvidence(value, "missing")
    else:
        raw = value
    try:
        text = _text(raw).replace("€", "").replace(" ", "")
        if "," in text and "." in text:
            text = text.replace(".", "").replace(",", ".") if text.rfind(",") > text.rfind(".") else text.replace(",", "")
        else:
            text = text.replace(",", ".")
        amount = Decimal(text)
        if not amount.is_finite() or amount < 0:
            raise InvalidOperation
    except (InvalidOperation, ValueError):
        return PriceEvidence(value, "malformed", "price is not a non-negative decimal")
    return PriceEvidence(value, "valid", None)


def normalize_listing(
    retailer: str,
    record: Mapping[str, Any],
    *,
    aliases: Mapping[str, str] | Sequence[str] | None = None,
) -> NormalizedListing:
    """Normalize one retailer listing while retaining source evidence."""
    name = _text(_first(record, "productName", "name", "title", "product_name"))
    alias_map = (
        aliases if isinstance(aliases, Mapping)
        else {alias: name for alias in (aliases or ())}
    )
    canonical_name = _canonical_text(name, alias_map)
    raw_attributes, structured = _structured_attributes(record)
    raw_named = _name_attributes(name)
    named = _name_attributes(_apply_aliases(name, alias_map))
    attributes: dict[str, Any] = {}
    basis: dict[str, str] = {}
    conflicts: dict[str, Mapping[str, Any]] = {}
    for key in _ATTRIBUTES:
        structured_value = structured.get(key)
        name_value = named.get(key)
        if structured_value is not None and name_value is not None and structured_value != name_value:
            conflicts[key] = {"structured": structured_value, "name": name_value}
        if structured_value is not None:
            attributes[key] = (
                _canonical_text(structured_value, alias_map)
                if key in {"brand", "variant", "package_type"}
                else structured_value
            )
            basis[key] = "structured"
        elif name_value is not None:
            attributes[key] = (
                _canonical_text(name_value, alias_map)
                if key in {"brand", "variant", "package_type"}
                else name_value
            )
            basis[key] = "name"
    if attributes.get("total_volume_ml") is None and attributes.get("unit_size_ml") is not None and attributes.get("pack_count") is not None:
        attributes["total_volume_ml"] = attributes["unit_size_ml"] * attributes["pack_count"]
        basis["total_volume_ml"] = "derived: unit_size_ml * pack_count"
    elif attributes.get("unit_size_ml") is None and attributes.get("total_volume_ml") is not None and attributes.get("pack_count"):
        total = attributes["total_volume_ml"]
        if total % attributes["pack_count"] == 0:
            attributes["unit_size_ml"] = total // attributes["pack_count"]
            basis["unit_size_ml"] = "derived: total_volume_ml / pack_count"
    missing = tuple(key for key in _ATTRIBUTES if attributes.get(key) is None)
    identity, tier = _identity(retailer, record, name, attributes)
    raw_attributes = dict(raw_attributes)
    raw_attributes["name"] = name
    raw_attributes["name_values"] = raw_named
    raw_attributes["canonical_name_values"] = named
    return NormalizedListing(
        retailer=retailer,
        source_identity=identity,
        identity_tier=tier,
        name=name,
        canonical_name=canonical_name,
        raw_record=record,
        raw_attributes=raw_attributes,
        name_attributes=named,
        attributes=attributes,
        inference_basis=basis,
        conflicts=conflicts,
        missing_attributes=missing,
        price=_price(record),
    )


def _completeness(payload: Mapping[str, Any], count: int, limit: int | None = None) -> tuple[Completeness, dict[str, Any]]:
    pagination_value = payload.get("pagination")
    pagination = dict(pagination_value) if isinstance(pagination_value, Mapping) else {}
    for key in ("total", "offset", "page", "next", "hasMore", "has_more", "totalPages", "total_pages"):
        if key in payload and key not in pagination:
            pagination[key] = payload[key]
    has_more = pagination.get("hasMore", pagination.get("has_more"))
    if has_more is not None:
        return (not bool(has_more), pagination)
    if pagination.get("next"):
        return False, pagination
    total = pagination.get("total")
    offset = pagination.get("offset", 0)
    if isinstance(total, int) and isinstance(offset, int):
        return offset + count >= total, pagination
    return "unknown", pagination


def _records(payload: Mapping[str, Any], retailer: str) -> list[Mapping[str, Any]]:
    if retailer == "dunnes":
        # Fixture case: VTEX nests itemId and seller price below each product.
        products = payload.get("data", {}).get("productSearch", {}).get("products", [])
        result: list[Mapping[str, Any]] = []
        for product in products if isinstance(products, list) else []:
            if not isinstance(product, Mapping):
                continue
            items = product.get("items") or [{}]
            for item in items:
                merged = dict(product)
                if isinstance(item, Mapping):
                    merged.update(item)
                    sellers = item.get("sellers") or []
                    if sellers and isinstance(sellers[0], Mapping):
                        offer = sellers[0].get("commertialOffer")
                        if isinstance(offer, Mapping) and offer.get("Price") is not None:
                            merged["price"] = offer["Price"]
                merged["_source_product"] = product
                merged["_source_item"] = item
                result.append(merged)
        return result
    # Fixture cases: SuperValu uses items; Tesco uses hydrated products.
    products = payload.get("items") if retailer == "supervalu" else payload.get("products")
    return [item for item in products if isinstance(item, Mapping)] if isinstance(products, list) else []


class DiscoveryAdapter:
    retailer: str
    capabilities: CapabilityContract
    # Upper bound on outbound requests issued by one search() call, used for
    # budget pre-checks. Fixture case: tesco spends one search plus one batched
    # GraphQL hydration call.
    max_requests_per_search = 1

    def __init__(self, client: Callable[[str], Mapping[str, Any]], *, limit: int | None = None):
        self.client = client
        self.limit = limit

    @property
    def session_bootstrapped(self) -> bool:
        return True

    def _result(self, payload: Mapping[str, Any], events: Sequence[RequestEvent], aliases: Sequence[str] = ()) -> DiscoveryResult:
        records = _records(payload, self.retailer)
        complete, pagination = _completeness(payload, len(records), self.limit)
        listings = tuple(normalize_listing(self.retailer, record, aliases=aliases) for record in records)
        return DiscoveryResult(listings, complete, pagination, tuple(records), tuple(events))

    def search(self, pack: BenchmarkPack) -> DiscoveryResult:
        raise NotImplementedError

    def supports(self, identity_tier: str) -> bool:
        return self.capabilities.supports(identity_tier)

    def is_collectable(self, listing: NormalizedListing) -> bool:
        return self.supports(listing.identity_tier)


class DunnesDiscoveryAdapter(DiscoveryAdapter):
    retailer = "dunnes"
    capabilities = CapabilityContract({
        "composite": Capability("composite", True, "search + mapped item collection", "tested productReference:itemId collection path"),
    })

    def __init__(self, client: Callable[[str], Mapping[str, Any]] | None = None):
        if client is None:
            from .collector import DunnesClient
            client = DunnesClient()
        super().__init__(client, limit=50)

    def search(self, pack: BenchmarkPack) -> DiscoveryResult:
        payload = self.client(pack.search_term)
        return self._result(payload, (RequestEvent("search"),), pack.aliases)


class SuperValuDiscoveryAdapter(DiscoveryAdapter):
    retailer = "supervalu"

    def __init__(
        self,
        client: Callable[[str], Mapping[str, Any]],
        *,
        hydrator: Callable[[str], Mapping[str, Any]] | None = None,
    ):
        self.store_id = getattr(client, "store_id", None)
        self.hydrator = hydrator or getattr(client, "fetch_product", None)
        self.capabilities = CapabilityContract({
            "product": Capability(
                "product", self.hydrator is not None, "product-ID hydration",
                "tested product-ID hydration path" if self.hydrator else "no tested hydrator supplied",
            ),
        })
        self._bootstrapped = False
        super().__init__(client, limit=50)

    @property
    def session_bootstrapped(self) -> bool:
        return self._bootstrapped

    def search(self, pack: BenchmarkPack) -> DiscoveryResult:
        events = []
        scope = {"store_id": self.store_id} if self.store_id is not None else {}
        if not self._bootstrapped:
            events.append(RequestEvent("bootstrap", metadata=scope))
            self._bootstrapped = True
        payload = self.client(pack.search_term)
        return self._result(payload, (*events, RequestEvent("search", metadata=scope)), pack.aliases)

    def hydrate(self, product_id: str) -> DiscoveryResult:
        if self.hydrator is None:
            raise RuntimeError("SuperValu product-ID hydration is not supported")
        payload = self.hydrator(str(product_id))
        records = _records(payload, self.retailer)
        if not records and isinstance(payload, Mapping) and payload.get("productId"):
            records = [payload]
        return DiscoveryResult(
            tuple(normalize_listing(self.retailer, record) for record in records),
            True,
            {}, tuple(records), (RequestEvent("hydration"),),
        )


class TescoDiscoveryAdapter(DiscoveryAdapter):
    retailer = "tesco"
    max_requests_per_search = 2
    capabilities = CapabilityContract({
        "tpnb": Capability("tpnb", True, "search + GraphQL hydration", "tested known-TPNB direct path"),
    })

    def __init__(self, client: Callable[[str], Mapping[str, Any]], *, min_request_interval: float | None = None):
        if min_request_interval is not None and min_request_interval < 0:
            raise ValueError("Tesco request interval must not be negative")
        self.min_request_interval = min_request_interval
        super().__init__(client, limit=10)

    def search(self, pack: BenchmarkPack) -> DiscoveryResult:
        payload = self.client(pack.search_term)
        products = payload.get("products", []) if isinstance(payload, Mapping) else []
        events = [RequestEvent("search")]
        if products:
            events.append(RequestEvent("hydration", batch_size=len(products)))
        return self._result(payload, events, pack.aliases)

    def hydrate(self, tpnb: str) -> DiscoveryResult:
        fetcher = getattr(self.client, "fetch_product", None)
        if not callable(fetcher):
            raise RuntimeError("Tesco known-TPNB hydration is not supported")
        payload = fetcher(str(tpnb))
        return self._result(payload, (RequestEvent("hydration"),))


__all__ = [
    "Capability", "CapabilityContract", "DiscoveryAdapter", "DiscoveryResult", "DunnesDiscoveryAdapter",
    "NormalizedListing", "PriceEvidence", "RequestEvent", "SuperValuDiscoveryAdapter",
    "TescoDiscoveryAdapter", "normalize_listing",
]
