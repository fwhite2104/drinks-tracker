"""Aldi Ireland collection client over the Spryker Glue JSON API.

The storefront web edge (``www.aldi.ie``) sits behind Akamai, but the backing
catalog service is a plain, unauthenticated Spryker Glue JSON REST API behind
Azure APIM — no cookies, no session, no bot protection (admitted by
full-feed-coverage ticket 03; evidence in
``.scratch/full-feed-coverage/research/aldi/FINDINGS.md``).  Verified,
endpoints (all ``GET``, ``Content-Type: application/vnd.api+json``):

- ``{base}/v3/product-search?q=...&limit=..&offset=..`` — paginated bulk
  search returning fully priced products (this is the endpoint name the
  research timebox could not pin down; confirmed live 2026-08).
- ``{base}/v2/products?skus=SKU1,SKU2`` — priced batch lookup by SKU.
- ``{base}/products/{sku}?servicePoint=D001&serviceType=walk-in`` — priced
  single-SKU lookup (requires the service point).

Every response prices are integer euro cents plus euro display strings, so no
float conversion is ever needed; records keep the display strings.

This module supersedes the inline ``AldiClient`` stub in ``collector.py`` for
collection runs.  It keeps the exact same fetcher contract (``__call__`` for
searches, ``fetch_product`` for known-SKU hydration) so it is a drop-in for
``collect_aldi_one``, and adds the ``sellingSize`` pack parsing that the
ticket called for: strings like ``"1 L"``, ``"330 ML"`` or ``"6 x 330 ml"``
are parsed into pack-count / unit-size evidence for exact-pack validation.
"""

from __future__ import annotations

import re
import urllib.parse
import urllib.request
from decimal import Decimal
from typing import Any, Mapping

from . import source_http
from .money import euro_display

ALDI_API_BASE = "https://asl.api.aldi.ie/commerce"
ALDI_SEARCH_ENDPOINT = ALDI_API_BASE + "/v3/product-search"
ALDI_PRODUCT_ENDPOINT = ALDI_API_BASE + "/v2/products"
ALDI_SERVICE_POINT = "D001"  # merchants.json: every Aldi IE store, D001…
ALDI_SERVICE_TYPE = "walk-in"
ALDI_SEARCH_LIMIT = 30  # the API rejects page sizes outside {12,16,24,30,32,48,60}

_SELLING_SIZE_RE = re.compile(
    r"^\s*(?:(\d+)\s*[xX]\s*)?([0-9]+(?:[.,][0-9]+)?)\s*(ml|l)\s*$",
    re.IGNORECASE,
)


def parse_selling_size(text: Any) -> tuple[int, int] | None:
    """Parse one Aldi ``sellingSize`` string into pack evidence.

    Returns ``(pack_count, unit_size_ml)``.  ``sellingSize`` is the *total*
    selling size (e.g. ``"1.98 L"`` for a six-pack), so an explicit
    ``"N x S unit"`` prefix is the only reliable pack-count signal; with one,
    the size after the prefix is the unit size, without it the string is a
    single-item total.  Non-volume units (``"6 Each"``, ``"0.15 KG"``, ``"0.25 Kg drained"``) and
    anything unparseable return ``None`` rather than inventing a size.
    """
    if not isinstance(text, str):
        return None
    match = _SELLING_SIZE_RE.match(text)
    if match is None:
        return None
    pack_count = int(match.group(1)) if match.group(1) else 1
    try:
        volume = Decimal(match.group(2).replace(",", "."))
    except Exception:  # pragma: no cover - regex guarantees a decimal
        return None
    total_ml = volume * (Decimal(1000) if match.group(3).lower() == "l" else Decimal(1))
    # Without a "N x" prefix the string is the total selling size of a
    # single-item pack; with one, the size after the prefix is the unit size.
    unit_size_ml = int(total_ml)
    if pack_count < 1 or unit_size_ml < 1:
        return None
    return pack_count, unit_size_ml


def _cents_to_euro_display(cents: Any) -> str:
    """Render integer euro cents as a euro display string (ROUND_HALF_UP)."""
    return euro_display(Decimal(str(cents)) / 100)


def _aldi_record(item: Mapping[str, Any]) -> dict[str, Any]:
    """Normalize one Aldi Glue product into a flat listing record.

    Mirrors ``collector._aldi_record`` so ``collect_aldi_one`` can consume the
    record unchanged, and adds parsed pack evidence from ``sellingSize``.  The
    bottle deposit is only retained when the source reports a positive amount,
    because ``0`` means the listing carries no deposit rather than a zero
    deposit.
    """
    price = item.get("price")
    price = price if isinstance(price, Mapping) else {}
    record: dict[str, Any] = {
        "productId": str(item.get("sku") or ""),
        "name": str(item.get("name") or ""),
    }
    if item.get("brandName"):
        record["brand"] = item["brandName"]
    if price.get("amountRelevantDisplay") is not None:
        record["price"] = price["amountRelevantDisplay"]
    elif price.get("amount") is not None:
        record["price"] = _cents_to_euro_display(price["amount"])
    if price.get("wasPriceDisplay"):
        record["oldPrice"] = price["wasPriceDisplay"]
    if price.get("comparisonDisplay"):
        record["unitPriceText"] = price["comparisonDisplay"]
    selling_size = item.get("sellingSize")
    if selling_size is not None:
        # sellingSize is the total selling size, so it feeds total-volume
        # evidence, not unit-size evidence; parsed pack counts only come from
        # an explicit "N x S unit" string.
        record["totalVolume"] = selling_size
        parsed = parse_selling_size(selling_size)
        if parsed is not None:
            pack_count, unit_size_ml = parsed
            record["packCount"] = pack_count
            record["unitSizeMl"] = unit_size_ml
    if price.get("bottleDeposit"):
        record["bottleDepositText"] = (
            price.get("bottleDepositDisplay")
            or _cents_to_euro_display(price["bottleDeposit"])
        )
    return record


class AldiClient:
    """Fetch Aldi Ireland grocery search results and priced product details.

    The Glue API needs no auth and tolerates a minimal header set, but the
    priced endpoints are documented with a service point (an Aldi IE store
    reference); it is sent on every request so the client also works against
    endpoints that require it (``/products/{sku}``).  Requests are throttled
    to ``min_request_interval`` seconds and politeness matters even on an
    open endpoint.
    """

    def __init__(
        self,
        service_point: str = ALDI_SERVICE_POINT,
        search_endpoint: str = ALDI_SEARCH_ENDPOINT,
        product_endpoint: str = ALDI_PRODUCT_ENDPOINT,
        opener: urllib.request.OpenerDirector | None = None,
        min_request_interval: float = 1.0,
    ):
        if not service_point.strip():
            raise ValueError("Aldi service_point must not be empty")
        if min_request_interval < 0:
            raise ValueError("Aldi request interval must not be negative")
        self.service_point = service_point
        self.search_endpoint = search_endpoint
        self.product_endpoint = product_endpoint
        self._transport = source_http.RetailerTransport(
            "Aldi", opener=opener, min_request_interval=min_request_interval
        )

    def __call__(self, search_term: str) -> dict[str, Any]:
        """Search Aldi Ireland products; returns priced normalized records."""
        if not search_term.strip():
            raise ValueError("Aldi search term must not be empty")
        payload = self._transport.json(
            self.search_endpoint + "?" + urllib.parse.urlencode({
                "currency": "EUR",
                "serviceType": ALDI_SERVICE_TYPE,
                "servicePoint": self.service_point,
                "q": search_term,
                "limit": str(ALDI_SEARCH_LIMIT),
                "offset": "0",
                "sort": "relevance",
            })
        )
        if not isinstance(payload, dict):
            raise RuntimeError("Aldi search response was not a JSON object")
        items = payload.get("data")
        if not isinstance(items, list):
            raise RuntimeError("Aldi search response has no data list")
        meta_raw = payload.get("meta")
        meta: Mapping[str, Any] = meta_raw if isinstance(meta_raw, Mapping) else {}
        pagination_raw = meta.get("pagination")
        pagination: Mapping[str, Any] = (
            pagination_raw if isinstance(pagination_raw, Mapping) else {}
        )
        return {
            "items": [_aldi_record(item) for item in items if isinstance(item, Mapping)],
            "pagination": {
                "total": pagination.get("totalCount"),
                "offset": pagination.get("offset", 0),
            },
        }

    def fetch_product(self, product_id: str) -> dict[str, Any]:
        """Fetch priced products by SKU (batch endpoint, one SKU here)."""
        identifier = str(product_id).strip()
        if not identifier:
            raise ValueError("Aldi product_id must not be empty")
        payload = self._transport.json(
            self.product_endpoint + "?" + urllib.parse.urlencode({
                "serviceType": ALDI_SERVICE_TYPE,
                "servicePoint": self.service_point,
                "skus": identifier,
                "limit": str(ALDI_SEARCH_LIMIT),
            })
        )
        if not isinstance(payload, dict):
            raise RuntimeError("Aldi product response was not a JSON object")
        items = payload.get("data")
        records = [
            _aldi_record(item)
            for item in (items if isinstance(items, list) else [])
            if isinstance(item, Mapping)
        ]
        matching = [r for r in records if r["productId"] == identifier]
        return {"items": matching}


# ---------------------------------------------------------------------------
# Discovery-pipeline client (relocated verbatim from the inline stub in
# collector.py).  Collection runs use ``AldiClient`` above; the discovery
# adapters (``discovery_adapters.AldiDiscoveryAdapter``) still drive search +
# SKU hydration through this first-generation client, whose behavior is
# preserved unchanged.  ``collector`` re-exports it as ``AldiClient`` for the
# existing ``from .collector import AldiClient`` importers.
# ---------------------------------------------------------------------------


def _aldi_discovery_record(item: Mapping[str, Any]) -> dict[str, Any]:
    """Normalize one Aldi product into a flat listing record.

    Source prices are integer cents; the euro display strings are retained
    instead so no float conversion is ever needed.  The bottle deposit is
    only retained when the source reports a positive amount, because ``0``
    means the listing carries no deposit rather than a zero deposit.
    """
    price = item.get("price")
    price = price if isinstance(price, Mapping) else {}
    record: dict[str, Any] = {
        "productId": str(item.get("sku") or ""),
        "name": str(item.get("name") or ""),
    }
    if item.get("brandName"):
        record["brand"] = item["brandName"]
    if price.get("amountRelevantDisplay") is not None:
        record["price"] = price["amountRelevantDisplay"]
    elif price.get("amount") is not None:
        record["price"] = euro_display(Decimal(str(price["amount"])) / 100)
    if price.get("wasPriceDisplay"):
        record["oldPrice"] = price["wasPriceDisplay"]
    if price.get("comparisonDisplay"):
        record["unitPriceText"] = price["comparisonDisplay"]
    if item.get("sellingSize") is not None:
        # sellingSize is the total selling size (e.g. "1.98 L" for a 6-pack),
        # so it feeds the total-volume evidence, not the unit-size evidence.
        record["totalVolume"] = item["sellingSize"]
    if price.get("bottleDeposit"):
        record["bottleDepositText"] = (
            price.get("bottleDepositDisplay")
            or euro_display(Decimal(str(price["bottleDeposit"])) / 100)
        )
    return record


class AldiDiscoveryClient:
    """Fetch Aldi Ireland grocery search results and product details.

    Source limitations (validated against live responses): the storefront
    pages at ``groceries.aldi.ie`` block non-browser clients (HTTP 403), but
    the underlying ``asl.api.aldi.ie`` JSON endpoints answer directly.  Each
    search returns one page of at most ``ALDI_SEARCH_LIMIT`` items and the
    client does not paginate further.  No loyalty price is exposed by this
    source, so ``clubcard_price`` stays ``None``.
    """

    def __init__(
        self,
        search_endpoint: str = ALDI_SEARCH_ENDPOINT,
        product_endpoint: str = ALDI_PRODUCT_ENDPOINT,
        opener: urllib.request.OpenerDirector | None = None,
        min_request_interval: float = 1.0,
    ):
        if min_request_interval < 0:
            raise ValueError("Aldi request interval must not be negative")
        self.search_endpoint = search_endpoint
        self.product_endpoint = product_endpoint
        self._transport = source_http.RetailerTransport(
            "Aldi", opener=opener, min_request_interval=min_request_interval
        )

    def __call__(self, search_term: str) -> dict[str, Any]:
        """Search Aldi Ireland products."""
        if not search_term.strip():
            raise ValueError("Aldi search term must not be empty")
        payload = self._transport.json(
            self.search_endpoint + "?" + urllib.parse.urlencode({
                "currency": "EUR",
                "serviceType": "walk-in",
                "q": search_term,
                "limit": str(ALDI_SEARCH_LIMIT),
                "offset": "0",
                "sort": "relevance",
            })
        )
        if not isinstance(payload, dict):
            raise RuntimeError("Aldi search response was not a JSON object")
        items = payload.get("data")
        if not isinstance(items, list):
            raise RuntimeError("Aldi search response has no data list")
        meta_raw = payload.get("meta")
        meta: Mapping[str, Any] = meta_raw if isinstance(meta_raw, Mapping) else {}
        pagination_raw = meta.get("pagination")
        pagination: Mapping[str, Any] = (
            pagination_raw if isinstance(pagination_raw, Mapping) else {}
        )
        return {
            "items": [
                _aldi_discovery_record(item)
                for item in items
                if isinstance(item, Mapping)
            ],
            "pagination": {
                "total": pagination.get("totalCount"),
                "offset": pagination.get("offset", 0),
            },
        }

    def fetch_product(self, product_id: str) -> dict[str, Any]:
        """Fetch one Aldi product by its numeric SKU."""
        identifier = str(product_id).strip()
        if not identifier:
            raise ValueError("Aldi product_id must not be empty")
        payload = self._transport.json(
            self.product_endpoint + "?" + urllib.parse.urlencode({
                "serviceType": "walk-in",
                "skus": identifier,
                "limit": str(ALDI_SEARCH_LIMIT),
            })
        )
        if not isinstance(payload, dict):
            raise RuntimeError("Aldi product response was not a JSON object")
        items = payload.get("data")
        records = [
            _aldi_discovery_record(item)
            for item in (items if isinstance(items, list) else [])
            if isinstance(item, Mapping)
        ]
        matching = [r for r in records if r["productId"] == identifier]
        return {"items": matching}
