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
import time
import urllib.parse
import urllib.request
from decimal import ROUND_HALF_UP, Decimal
from typing import Any, Mapping

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
    return "€{}".format(Decimal(str(value)).quantize(Decimal("0.01"), ROUND_HALF_UP))


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
        self.opener = opener or urllib.request.build_opener()
        self.min_request_interval = min_request_interval
        self._last_request_at: float | None = None

    def __call__(self, search_term: str) -> dict[str, Any]:
        """Search Lidl Ireland products; returns normalized listing records."""
        if not search_term.strip():
            raise ValueError("Lidl search term must not be empty")
        payload = self._request_json(
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
        payload = self._request_json(
            self.detail_url_template.format(product_id=identifier)
        )
        if not isinstance(payload, dict):
            raise RuntimeError("Lidl detail response was not a JSON object")
        if not payload.get("havingPrice") or not isinstance(payload.get("price"), Mapping):
            # A resolvable but unpriced listing carries no observation.
            return {"items": []}
        return {"items": [_lidl_record(payload)]}

    def _throttle(self) -> None:
        if self._last_request_at is not None:
            delay = self.min_request_interval - (time.monotonic() - self._last_request_at)
            if delay > 0:
                time.sleep(delay)

    def _request_json(self, url: str, *, accept: str = "application/json") -> Any:
        self._throttle()
        request = urllib.request.Request(
            url,
            headers={"Accept": accept, "User-Agent": "drinks-tracker/0.1"},
        )
        try:
            with self.opener.open(request, timeout=30) as response:
                if getattr(response, "status", 200) >= 400:
                    raise RuntimeError(f"Lidl HTTP {response.status}")
                body = response.read()
        except Exception as exc:
            if isinstance(exc, RuntimeError):
                raise
            raise RuntimeError(f"Lidl request failed: {exc}") from exc
        finally:
            self._last_request_at = time.monotonic()
        try:
            return json.loads(body.decode("utf-8"))
        except (UnicodeDecodeError, ValueError) as exc:
            raise RuntimeError(f"Lidl response was not valid JSON: {exc}") from exc
