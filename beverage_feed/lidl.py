"""Lidl Ireland collection client over the undocumented storefront search API.

Lidl IE's category pages are backed by a plain, unauthenticated REST search
API — no cookies, no session, no bot protection (admitted by full-feed-coverage
ticket 02; evidence in ``.scratch/full-feed-coverage/research/lidl/``).
Endpoints (both ``GET``, plain JSON):

- ``{base}/q/api/search`` — search/category listing. Required params:
  ``q``, ``locale=en_IE`` (underscore form), ``assortment=IE`` (case
  sensitive), ``version=2.1.0`` (load-bearing; omitting it yields an opaque
  Spring 400), plus ``fetchsize``/``offset`` for paging. Results are
  ``items[].gridbox.data`` tiles (title, EUR price, availability windows,
  ``productId``) with the GTIN in ``gridbox.meta.ean``.
- ``{base}/p/api/detail/{productId}/IE/en`` — per-product detail: EANs,
  price block, stock badges.

Pack size is structurally available **nowhere** on lidl.ie — neither the
listing payload, nor the detail API, nor schema.org markup carries it (verified
against the research captures). Titles are the only in-source signal, so
``parse_title_pack`` parses them conservatively: no size in the title means no
invented pack evidence.

This module supersedes the inline ``LidlClient`` stub in ``collector.py`` for
collection runs. It keeps the same fetcher contract (``__call__`` for
searches, ``fetch_product`` for known-product hydration) so it is a drop-in
for ``collect_lidl_one``, and records price-window evidence through the raw
``specialTaxes``/``basePrice`` passthroughs the deposit extraction expects.
"""

from __future__ import annotations

import json
import re
import urllib.parse
import urllib.request
from decimal import Decimal
from typing import Any, Mapping

from . import source_http
from .money import euro_display

LIDL_API_BASE = "https://www.lidl.ie"
LIDL_SEARCH_ENDPOINT = LIDL_API_BASE + "/q/api/search"
LIDL_DETAIL_URL_TEMPLATE = LIDL_API_BASE + "/p/api/detail/{product_id}/IE/en"
LIDL_LOCALE = "en_IE"  # must be the underscore form, else "Locale not supported"
LIDL_ASSORTMENT = "IE"  # case-sensitive, else "Assortment 'null' is not supported"
LIDL_API_VERSION = "2.1.0"  # load-bearing: omitting it yields a Spring 400
LIDL_FETCH_SIZE = 48  # the site's own page size
LIDL_SEARCH_ACCEPT = "application/mindshift.search+json"

# Titles are the only in-source pack-size signal. Forms seen in drink titles:
# "6 x 330ml", "330ml Cans x6", "1.5L Bottle", and (most often on Lidl IE)
# no size at all. Each regex pins a volume to word boundaries so fragments
# like "6 Pack" or "2 for 1" can never invent a size.
_PACK_PREFIX_RE = re.compile(
    r"(?<![\w.])(\d+)\s*[xX]\s*([0-9]+(?:[.,][0-9]+)?)\s*(ml|l)(?![\w])",
    re.IGNORECASE,
)
_PACK_SUFFIX_RE = re.compile(
    r"(?<![\w.])([0-9]+(?:[.,][0-9]+)?)\s*(ml|l)[\w\s]{0,12}?[xX]\s*(\d+)(?![\w])",
    re.IGNORECASE,
)
_PACK_SIZE_RE = re.compile(
    r"(?<![\w.])([0-9]+(?:[.,][0-9]+)?)\s*(ml|l)(?![\w])",
    re.IGNORECASE,
)


def _pack_from_parts(count: int, amount: str, unit: str) -> tuple[int, int] | None:
    """Convert ``(pack_count, volume_amount, unit)`` into pack evidence."""
    try:
        volume = Decimal(amount.replace(",", "."))
    except Exception:  # pragma: no cover - the regexes guarantee a decimal
        return None
    total_ml = volume * (Decimal(1000) if unit.lower() == "l" else Decimal(1))
    unit_size_ml = int(total_ml)
    if count < 1 or unit_size_ml < 1:
        return None
    return count, unit_size_ml


def parse_title_pack(text: Any) -> tuple[int, int] | None:
    """Parse pack evidence ``(pack_count, unit_size_ml)`` from a Lidl title.

    Lidl exposes no structured pack size anywhere (confirmed against the
    research captures: the listing payload, the detail API, and schema.org
    markup all lack it), so titles are the only in-source signal and this
    parser is deliberately conservative — an explicit ``"N x S unit"``
    prefix or a ``"S unit ... x N"`` suffix yields the pack count, a bare
    volume yields a single-item pack, and anything unparseable returns
    ``None`` rather than inventing a size.
    """
    if not isinstance(text, str):
        return None
    prefix = _PACK_PREFIX_RE.search(text)
    if prefix is not None:
        return _pack_from_parts(int(prefix.group(1)), prefix.group(2), prefix.group(3))
    suffix = _PACK_SUFFIX_RE.search(text)
    if suffix is not None:
        return _pack_from_parts(int(suffix.group(3)), suffix.group(1), suffix.group(2))
    size = _PACK_SIZE_RE.search(text)
    if size is not None:
        return _pack_from_parts(1, size.group(1), size.group(2))
    return None


def _euro_price_display(value: Any) -> str:
    """Render a euro amount from the source (a JSON float) as a display string."""
    return euro_display(Decimal(str(value)))


def _lidl_record(
    data: Mapping[str, Any], meta: Mapping[str, Any] | None = None
) -> dict[str, Any]:
    """Normalize one Lidl tile (search gridbox ``data`` or detail payload).

    Mirrors ``collector._lidl_search_record`` so ``collect_lidl_one`` can
    consume the record unchanged, and adds ``gtin`` plus parsed pack
    evidence from the title when one is present.
    """
    price_raw = data.get("price")
    price = price_raw if isinstance(price_raw, Mapping) else {}
    keyfacts_raw = data.get("keyfacts")
    keyfacts = keyfacts_raw if isinstance(keyfacts_raw, Mapping) else {}
    name = str(
        keyfacts.get("title")
        or keyfacts.get("fullTitle")
        or data.get("title")
        or data.get("fullTitle")
        or ""
    )
    record: dict[str, Any] = {
        "productId": str(data.get("productId") or data.get("erpNumber") or ""),
        "name": name,
    }
    if price.get("price") is not None:
        record["price"] = _euro_price_display(price["price"])
    if price.get("oldPrice") is not None:
        record["oldPrice"] = (
            _euro_price_display(price["oldPrice"])
            if isinstance(price["oldPrice"], (int, float))
            else price["oldPrice"]
        )
    if isinstance(price.get("specialTaxes"), list):
        record["specialTaxes"] = price["specialTaxes"]
    base_price = price.get("basePrice")
    if isinstance(base_price, Mapping) and base_price.get("text") is not None:
        record["basePriceText"] = base_price["text"]
    packaging = price.get("packaging")
    if isinstance(packaging, Mapping) and packaging.get("text") is not None:
        record["packSize"] = packaging["text"]
    if data.get("canonicalPath"):
        record["url"] = data["canonicalPath"]
    if data.get("multipack") is not None:
        record["multipack"] = data["multipack"]
    gtin = (meta or {}).get("ean")
    eans = data.get("eans")
    if gtin is None and isinstance(eans, list) and eans:
        gtin = eans[0]  # detail payloads carry EANs instead of gridbox meta
    if gtin:
        record["gtin"] = str(gtin)
    parsed = parse_title_pack(name)
    if parsed is not None:
        record["packCount"], record["unitSizeMl"] = parsed
    return record


class LidlClient:
    """Fetch Lidl Ireland search results and product details.

    The hidden search API needs no auth and tolerates a minimal header set,
    but ``version``/``locale``/``assortment`` are load-bearing (see the
    research notes), so they are sent on every search. Requests are
    throttled to ``min_request_interval`` seconds; politeness matters even
    on an open endpoint.
    """

    def __init__(
        self,
        search_endpoint: str = LIDL_SEARCH_ENDPOINT,
        detail_url_template: str = LIDL_DETAIL_URL_TEMPLATE,
        opener: urllib.request.OpenerDirector | None = None,
        min_request_interval: float = 1.0,
    ):
        if min_request_interval < 0:
            raise ValueError("Lidl request interval must not be negative")
        if "{product_id}" not in detail_url_template:
            raise ValueError("Lidl detail URL template must contain {product_id}")
        self.search_endpoint = search_endpoint
        self.detail_url_template = detail_url_template
        self._transport = source_http.RetailerTransport(
            "Lidl", opener=opener, min_request_interval=min_request_interval
        )

    def __call__(self, search_term: str) -> dict[str, Any]:
        """Search Lidl Ireland products; returns normalized listing records."""
        if not search_term.strip():
            raise ValueError("Lidl search term must not be empty")
        payload = self._transport.json(
            self.search_endpoint + "?" + urllib.parse.urlencode({
                "q": search_term,
                "fetchsize": str(LIDL_FETCH_SIZE),
                "offset": "0",
                "locale": LIDL_LOCALE,
                "assortment": LIDL_ASSORTMENT,
                "version": LIDL_API_VERSION,
            }),
            accept=LIDL_SEARCH_ACCEPT,
        )
        if not isinstance(payload, dict):
            raise RuntimeError("Lidl search response was not a JSON object")
        items = payload.get("items")
        if items is None and payload.get("resultType") in {"empty", "redirect"}:
            # Observed live: empty and redirect results carry no items list.
            items = []
        if not isinstance(items, list):
            raise RuntimeError("Lidl search response has no items list")
        records = []
        for item in items:
            if not isinstance(item, Mapping):
                continue
            gridbox = item.get("gridbox")
            gridbox = gridbox if isinstance(gridbox, Mapping) else {}
            data = gridbox.get("data")
            data = data if isinstance(data, Mapping) else {}
            meta = gridbox.get("meta")
            meta = meta if isinstance(meta, Mapping) else None
            records.append(_lidl_record(data, meta))
        total = payload.get("numFound")
        if total is None and payload.get("resultType") == "empty":
            total = 0  # an empty result set is a complete result set
        return {
            "items": records,
            "pagination": {"total": total, "offset": payload.get("offset", 0)},
        }

    def fetch_product(self, product_id: str) -> dict[str, Any]:
        """Fetch one product's priced detail by its Lidl productId."""
        identifier = str(product_id).strip()
        if not identifier:
            raise ValueError("Lidl product_id must not be empty")
        payload = self._transport.json(
            self.detail_url_template.format(product_id=identifier)
        )
        if not isinstance(payload, dict):
            raise RuntimeError("Lidl detail response was not a JSON object")
        if not payload.get("havingPrice") or not isinstance(payload.get("price"), Mapping):
            # A resolvable but unpriced listing carries no observation.
            return {"items": []}
        return {"items": [_lidl_record(payload)]}


# ---------------------------------------------------------------------------
# Discovery-pipeline client (relocated verbatim from the inline stub in
# collector.py).  Collection runs use ``LidlClient`` above; the discovery
# adapters (``discovery_adapters.LidlDiscoveryAdapter``) still drive search +
# product-page hydration through this first-generation client, whose behavior
# is preserved unchanged.  ``collector`` re-exports it as ``LidlClient`` for
# the existing ``from .collector import LidlClient`` importers.
# ---------------------------------------------------------------------------

_LIDL_PDP_SCRIPT = re.compile(
    r'<script type="application/json" data-nuxt-data="pdp-view"[^>]*>(.*?)</script>',
    re.S,
)


def _lidl_price_record(price: Mapping[str, Any] | None) -> dict[str, Any]:
    """Flatten one Lidl price block into retained source-evidence fields."""
    price = price if isinstance(price, Mapping) else {}
    record: dict[str, Any] = {"specialTaxes": price.get("specialTaxes") or []}
    base_price = price.get("basePrice")
    if isinstance(base_price, Mapping) and base_price.get("text") is not None:
        record["basePriceText"] = base_price["text"]
    if price.get("price") is not None:
        record["price"] = price["price"]
    if price.get("oldPrice"):
        record["oldPrice"] = price["oldPrice"]
    packaging = price.get("packaging")
    if isinstance(packaging, Mapping) and packaging.get("text") is not None:
        record["packSize"] = packaging["text"]
    return record


def _lidl_search_record(item: Mapping[str, Any]) -> dict[str, Any]:
    """Normalize one search gridbox item into a flat listing record."""
    gridbox = item.get("gridbox")
    data = gridbox.get("data") if isinstance(gridbox, Mapping) else {}
    if not isinstance(data, Mapping):
        data = {}
    keyfacts_raw = data.get("keyfacts")
    keyfacts: Mapping[str, Any] = keyfacts_raw if isinstance(keyfacts_raw, Mapping) else {}
    name = (
        data.get("fullTitle") or keyfacts.get("fullTitle")
        or data.get("title") or keyfacts.get("title") or ""
    )
    product_id = data.get("productId") or data.get("erpNumber") or item.get("code") or ""
    record = _lidl_price_record(data.get("price"))
    record["productId"] = str(product_id)
    record["name"] = str(name)
    if data.get("canonicalPath"):
        record["url"] = data["canonicalPath"]
    if data.get("multipack") is not None:
        record["multipack"] = data["multipack"]
    return record


def _lidl_deref(elements: list[Any], index: Any, depth: int = 0) -> Any:
    """Resolve one Nuxt ``__NUXT_DATA__`` reference against the payload array."""
    if not isinstance(index, int) or depth > 50:
        return index
    if index < 0 or index >= len(elements):
        return index
    value = elements[index]
    if isinstance(value, dict):
        return {key: _lidl_deref(elements, item, depth + 1) for key, item in value.items()}
    if isinstance(value, list):
        return [_lidl_deref(elements, item, depth + 1) for item in value]
    return value


class LidlDiscoveryClient:
    """Fetch Lidl Ireland search results and product pages.

    Source limitations (validated against live responses): the documented
    ``/p/api`` endpoints return 404; the working search API is
    ``/q/api/search`` and requires ``Accept: application/mindshift.search+json``.
    Each search returns one page of up to ``fetchsize`` items; the client does
    not paginate beyond the first page.  Lidl Plus loyalty prices are not
    exposed by this source, so ``clubcard_price`` stays ``None``.
    """

    def __init__(
        self,
        endpoint: str = LIDL_SEARCH_ENDPOINT,
        base_url: str = LIDL_API_BASE,
        opener: urllib.request.OpenerDirector | None = None,
        min_request_interval: float = 1.0,
    ):
        self.endpoint = endpoint
        self.base_url = base_url.rstrip("/")
        self._transport = source_http.RetailerTransport(
            "Lidl", opener=opener, min_request_interval=min_request_interval
        )

    def __call__(self, search_term: str) -> dict[str, Any]:
        """Search Lidl Ireland products."""
        if not search_term.strip():
            raise ValueError("Lidl search term must not be empty")
        payload = self._transport.json(
            self.endpoint + "?" + urllib.parse.urlencode({
                "assortment": "IE",
                "locale": "en_IE",
                "q": search_term,
                "version": "2.1.1",
                "fetchsize": "100",
            }),
            accept=LIDL_SEARCH_ACCEPT,
        )
        return self._normalize_search_payload(payload)

    def fetch_category_page(self, offset: int, category_id: str) -> dict[str, Any]:
        """Fetch one page of a category listing (list-only category walk).

        Verified live shape (research ticket 02, ``research/lidl/NOTES.md``):
        the search API accepts ``category.id`` with an empty ``q`` for pure
        category browse, and ``offset`` pages through ``numFound``. Returns
        the same client-normalized payload shape as search.
        """
        if offset < 0:
            raise ValueError("Lidl category offset must not be negative")
        if not category_id.strip():
            raise ValueError("Lidl category_id must not be empty")
        payload = self._transport.json(
            self.endpoint + "?" + urllib.parse.urlencode({
                "assortment": "IE",
                "locale": "en_IE",
                "q": "",
                "category.id": category_id,
                "version": "2.1.1",
                "fetchsize": "100",
                "offset": str(offset),
            }),
            accept=LIDL_SEARCH_ACCEPT,
        )
        return self._normalize_search_payload(payload)

    def _normalize_search_payload(self, payload: Any) -> dict[str, Any]:
        """Flatten a raw search/category payload into items + pagination."""
        if not isinstance(payload, dict):
            raise RuntimeError("Lidl search response was not a JSON object")
        items = payload.get("items")
        if items is None and payload.get("resultType") in {"empty", "redirect"}:
            # Observed live: empty and redirect results carry no items list.
            items = []
        if not isinstance(items, list):
            raise RuntimeError("Lidl search response has no items list")
        records = [
            _lidl_search_record(item)
            for item in items
            if isinstance(item, Mapping)
        ]
        total: Any = payload.get("numFound")
        if total is None and payload.get("resultType") == "empty":
            total = 0  # an empty result set is a complete result set
        return {
            "items": records,
            "pagination": {"total": total, "offset": payload.get("offset", 0)},
        }

    def fetch_product(self, product_id: str) -> dict[str, Any]:
        """Fetch one Lidl product where supported.

        Lidl has no JSON product endpoint.  The product ID resolves through
        the search API's redirect to a server-rendered product page whose
        embedded Nuxt data carries the same price evidence.  Returns
        ``{"items": []}`` when the ID does not resolve to that product's
        own page.
        """
        identifier = str(product_id).strip()
        if not identifier:
            raise ValueError("Lidl product_id must not be empty")
        redirect = self._transport.json(
            self.endpoint + "?" + urllib.parse.urlencode({
                "assortment": "IE",
                "locale": "en_IE",
                "q": identifier,
                "version": "2.1.1",
            }),
            accept=LIDL_SEARCH_ACCEPT,
        )
        if not isinstance(redirect, dict):
            raise RuntimeError("Lidl product lookup was not a JSON object")
        path = redirect.get("redirectURL")
        if not isinstance(path, str) or not path:
            return {"items": []}
        html = self._transport.text(self.base_url + path, accept="text/html")
        record = self._record_from_product_page(identifier, html)
        return {"items": [record]} if record else {"items": []}

    def _record_from_product_page(
        self, product_id: str, html: str
    ) -> dict[str, Any] | None:
        """Extract one listing record from a Lidl product page."""
        match = _LIDL_PDP_SCRIPT.search(html)
        if match is None:
            raise RuntimeError("Lidl product page has no embedded product data")
        try:
            elements = json.loads(match.group(1))
        except ValueError as exc:
            raise RuntimeError(f"Lidl product page data is invalid JSON: {exc}") from exc
        if not isinstance(elements, list):
            raise RuntimeError("Lidl product page data has an unexpected shape")
        product: dict[str, Any] = {}
        price_block: dict[str, Any] | None = None
        for element in elements:
            if not isinstance(element, dict):
                continue
            if "erpNumber" in element and not product:
                candidate_id = _lidl_deref(
                    elements, element.get("productId", element.get("erpNumber"))
                )
                if str(candidate_id or "") == product_id:
                    product = {
                        key: _lidl_deref(elements, element[key])
                        for key in ("keyfacts", "canonicalPath", "multipack")
                        if key in element
                    }
            if "specialTaxes" in element and "price" in element:
                resolved = {
                    key: _lidl_deref(elements, value) for key, value in element.items()
                }
                if not isinstance(resolved.get("price"), (int, float)):
                    continue
                if "startDate" not in element:
                    # The plain block is the current price; dated blocks are
                    # campaign windows and only serve as a fallback.
                    price_block = resolved
                    break
                price_block = price_block or resolved
        if not product or price_block is None:
            return None
        keyfacts_raw = product.get("keyfacts")
        keyfacts: Mapping[str, Any] = keyfacts_raw if isinstance(keyfacts_raw, Mapping) else {}
        name = keyfacts.get("fullTitle") or keyfacts.get("title") or ""
        record = _lidl_price_record(price_block)
        record["productId"] = str(product_id)
        record["name"] = str(name)
        if product.get("canonicalPath"):
            record["url"] = product["canonicalPath"]
        if product.get("multipack") is not None:
            record["multipack"] = product["multipack"]
        return record
