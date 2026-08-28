"""The first local collection seam and Dunnes VTEX adapter."""

from __future__ import annotations

import argparse
import http.cookiejar
from contextlib import closing
import json
import logging
import os
import re
import sqlite3
import sys
import time
import urllib.parse
import urllib.request
import uuid
from dataclasses import dataclass, fields
from datetime import datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
from pathlib import Path
from typing import Any, Callable, Mapping


DUNNES_ENDPOINT = "https://storefrontgateway.dunnesstoresgrocery.com/api/stores"
DUNNES_STORE_ID = os.environ.get("DUNNES_STORE_ID", "258")

logger = logging.getLogger("beverage_feed.collector")


@dataclass(frozen=True)
class BenchmarkPack:
    catalog_id: str
    name: str
    brand: str
    variant: str
    pack_count: int
    unit_size_ml: int
    package_type: str
    search_term: str
    aliases: tuple[str, ...] = ()


@dataclass(frozen=True)
class DunnesMapping:
    catalog_id: str
    expected_product_name: str
    source_product_reference: str | None = None
    source_item_id: str | None = None
    status: str = "approved"


@dataclass(frozen=True)
class SuperValuMapping:
    catalog_id: str
    expected_product_name: str
    source_product_id: str | None = None
    status: str = "approved"


@dataclass(frozen=True)
class TescoMapping:
    catalog_id: str
    expected_product_name: str
    source_tpnb: str | None = None
    status: str = "approved"


@dataclass(frozen=True)
class LidlMapping:
    catalog_id: str
    expected_product_name: str
    source_product_id: str | None = None
    status: str = "approved"


@dataclass(frozen=True)
class AldiMapping:
    catalog_id: str
    expected_product_name: str
    source_product_id: str | None = None
    status: str = "approved"


class DunnesClient:
    """Search the Dunnes Stores *grocery* storefront gateway.

    The grocery site (dunnesstoresgrocery.com) exposes a JSON search API on a
    separate ``storefrontgateway`` host that is not Cloudflare-gated. Its item
    shape already carries ``Price`` and ``taxDetails`` keys compatible with the
    downstream VTEX-style parsing, so this client translates each result into
    the ``productSearch.products`` envelope the collector expects.
    """

    def __init__(self, endpoint: str = DUNNES_ENDPOINT, store_id: str = DUNNES_STORE_ID):
        self.endpoint = endpoint.rstrip("/")
        self.store_id = store_id

    def __call__(self, search_term: str) -> dict[str, Any]:
        if not search_term.strip():
            raise ValueError("Dunnes search term must not be empty")
        url = "{}/{}/search?{}".format(
            self.endpoint,
            self.store_id,
            urllib.parse.urlencode({"q": search_term, "take": 50}),
        )
        request = urllib.request.Request(
            url,
            headers={
                "Accept": "application/json",
                "User-Agent": "drinks-tracker/0.1",
            },
        )
        try:
            with urllib.request.urlopen(request, timeout=30) as response:
                if response.status >= 400:
                    raise RuntimeError(f"Dunnes HTTP {response.status}")
                payload = json.load(response)
        except Exception as exc:
            if isinstance(exc, RuntimeError):
                raise
            raise RuntimeError(f"Dunnes request failed: {exc}") from exc

        items = payload.get("items") if isinstance(payload, dict) else None
        if not isinstance(items, list):
            raise RuntimeError("Dunnes response has no items list")

        products: list[dict[str, Any]] = []
        for item in items:
            if not isinstance(item, dict):
                continue
            # Offer carries both the VTEX-style "Price" key the collector
            # reads and the gateway's own fields (taxDetails for DRS etc.).
            offer = dict(item)
            offer["Price"] = item.get("priceNumeric")
            offer["ListPrice"] = item.get("wasPriceNumeric")
            products.append(
                {
                    "productName": item.get("name", ""),
                    "productReference": item.get("productId", ""),
                    "items": [
                        {
                            "itemId": item.get("sku", ""),
                            "sellers": [{"commertialOffer": offer}],
                        }
                    ],
                }
            )
        return {"data": {"productSearch": {"products": products}}}


SCHEMA = """
CREATE TABLE IF NOT EXISTS catalog_packs (
    catalog_id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    brand TEXT NOT NULL,
    variant TEXT NOT NULL,
    pack_count INTEGER NOT NULL,
    unit_size_ml INTEGER NOT NULL,
    package_type TEXT NOT NULL,
    search_term TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS catalog_mappings (
    catalog_id TEXT NOT NULL,
    retailer TEXT NOT NULL,
    expected_product_name TEXT NOT NULL,
    source_product_reference TEXT,
    source_item_id TEXT,
    status TEXT NOT NULL,
    PRIMARY KEY (catalog_id, retailer),
    FOREIGN KEY (catalog_id) REFERENCES catalog_packs(catalog_id)
);
CREATE TABLE IF NOT EXISTS catalog_candidates (
    candidate_id TEXT PRIMARY KEY,
    retailer TEXT NOT NULL,
    source_product_reference TEXT NOT NULL,
    source_item_id TEXT NOT NULL,
    source_product_name TEXT NOT NULL,
    displayed_price TEXT,
    raw_record TEXT NOT NULL,
    status TEXT NOT NULL,
    first_seen_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS collection_runs (
    run_id TEXT PRIMARY KEY,
    started_at TEXT NOT NULL,
    finished_at TEXT NOT NULL,
    status TEXT NOT NULL,
    observed_count INTEGER NOT NULL,
    failed_count INTEGER NOT NULL,
    summary TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS collection_results (
    run_id TEXT NOT NULL,
    catalog_id TEXT NOT NULL,
    retailer TEXT NOT NULL,
    status TEXT NOT NULL,
    error TEXT,
    source_product_reference TEXT,
    source_item_id TEXT,
    source_scope TEXT,
    complete TEXT,
    recorded_at TEXT NOT NULL,
    PRIMARY KEY (run_id, catalog_id, retailer),
    FOREIGN KEY (run_id) REFERENCES collection_runs(run_id)
);
CREATE TABLE IF NOT EXISTS collection_diagnostics (
    diagnostic_id INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id TEXT NOT NULL,
    retailer TEXT NOT NULL,
    catalog_id TEXT,
    level TEXT NOT NULL,
    event TEXT NOT NULL,
    message TEXT,
    raw_record TEXT,
    request_metadata TEXT,
    created_at TEXT NOT NULL,
    FOREIGN KEY (run_id) REFERENCES collection_runs(run_id)
);
CREATE TABLE IF NOT EXISTS price_observations (
    observation_id INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id TEXT NOT NULL,
    catalog_id TEXT NOT NULL,
    retailer TEXT NOT NULL,
    source_product_reference TEXT NOT NULL,
    source_item_id TEXT NOT NULL,
    source_product_name TEXT NOT NULL,
    displayed_price TEXT NOT NULL,
    clubcard_price TEXT,
    drs_deposit TEXT,
    source_scope TEXT,
    currency TEXT NOT NULL,
    pack_count INTEGER NOT NULL,
    unit_size_ml INTEGER NOT NULL,
    package_type TEXT NOT NULL,
    component_unit_price TEXT,
    price_per_litre TEXT,
    observed_at TEXT NOT NULL,
    FOREIGN KEY (run_id) REFERENCES collection_runs(run_id)
);
CREATE TABLE IF NOT EXISTS retailers (
    retailer_slug TEXT PRIMARY KEY,
    display_name TEXT NOT NULL,
    tier INTEGER NOT NULL,
    country TEXT NOT NULL DEFAULT 'IE',
    data_source_type TEXT NOT NULL,
    active INTEGER NOT NULL DEFAULT 1
);
"""

# Tier 1 retailer registry, seeded idempotently by ensure_schema().
_RETAILER_SEED = (
    ("tesco", "Tesco Ireland", 1),
    ("dunnes", "Dunnes Stores", 1),
    ("supervalu", "SuperValu", 1),
    ("lidl", "Lidl Ireland", 1),
    ("aldi", "Aldi Ireland", 1),
)


def timestamp() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def _decimal_price(value: Any) -> Decimal:
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


def _decimal_text(value: Decimal, places: str = "0.01") -> str:
    return format(value.quantize(Decimal(places), rounding=ROUND_HALF_UP), "f")


def _page_completeness(payload: Mapping[str, Any] | None) -> str:
    """Classify a retailer response page as complete, truncated, or unknown.

    Retailer clients that normalize pagination metadata (``items`` plus
    ``pagination.total``/``pagination.offset``) let collection prove a page
    covered every match — the same completeness evidence discovery records
    per search. Sources without that evidence stay ``unknown`` rather than
    claiming a complete absence.
    """
    if not isinstance(payload, Mapping):
        return "unknown"
    pagination = payload.get("pagination")
    if not isinstance(pagination, Mapping):
        return "unknown"
    total = pagination.get("total")
    if not isinstance(total, int) or isinstance(total, bool):
        return "unknown"
    offset = pagination.get("offset", 0)
    if not isinstance(offset, int) or isinstance(offset, bool):
        offset = 0
    items = payload.get("items")
    seen = len(items) if isinstance(items, list) else 0
    return "true" if offset + seen >= total else "false"


def _absence_status(payload: Mapping[str, Any] | None) -> str:
    """Distinguish a proven ``not_found`` from an ``inconclusive`` page.

    A mapped product absent from a page that provably covered every match is
    genuinely not_found; absence from a truncated page is inconclusive and
    must never be recorded as a false absence.
    """
    return "inconclusive" if _page_completeness(payload) == "false" else "not_found"


_GENERIC_PACKAGE_TOKENS = {"can", "cans", "bottle", "bottles", "carton", "cartons", "pouch", "pouches"}


def _dunnes_drs_deposit(offer: Mapping[str, Any]) -> Decimal | None:
    """Extract the Dunnes refundable deposit from offer evidence.

    Source limitation (validated against live responses 2026-08): the VTEX
    GraphQL schema rejects ``taxDetails``, ``drsDeposit``, ``deposit`` and
    ``depositAmount`` as unknown fields (HTTP 400), while the valid ``Tax``
    and ``taxPercentage`` fields are always zero — so no live offer carries
    deposit evidence today.  The documented precedence is implemented anyway
    (a taxDetails record whose group/name identifies a deposit, then
    ``drsDeposit``, then ``deposit``, then ``depositAmount``) so a future
    source change needs no code change here; callers record a
    ``drs_not_available`` diagnostic when this returns ``None``.
    """
    tax_details = offer.get("taxDetails")
    if isinstance(tax_details, list):
        for detail in tax_details:
            if not isinstance(detail, Mapping):
                continue
            label = " ".join(
                str(detail.get(key) or "")
                for key in ("groupName", "group", "name", "type")
            )
            if "deposit" in label.lower():
                amount = detail.get("amount")
                if amount is None:
                    amount = detail.get("value")
                if amount is not None:
                    return _decimal_price(amount)
    return _optional_price(offer, "drsDeposit", "deposit", "depositAmount")


def _normalise_name(value: str) -> set[str]:
    return set(re.findall(r"[a-z0-9]+", value.lower().replace("-", " ")))


def _find_listing(
    payload: Mapping[str, Any], mapping: DunnesMapping
) -> tuple[dict[str, Any], dict[str, Any], dict[str, Any]]:
    products = payload.get("data", {}).get("productSearch", {}).get("products")
    if not isinstance(products, list):
        raise ValueError("Dunnes response has no productSearch.products list")

    expected_tokens = _normalise_name(mapping.expected_product_name)
    identity_matched = False
    for product in products:
        if not isinstance(product, dict):
            continue
        if (
            mapping.source_product_reference
            and product.get("productReference") != mapping.source_product_reference
        ):
            continue
        if (
            not mapping.source_product_reference
            and not expected_tokens.issubset(_normalise_name(product.get("productName", "")))
        ):
            continue
        items = product.get("items") or []
        for item in items:
            if mapping.source_item_id and item.get("itemId") != mapping.source_item_id:
                continue
            identity_matched = True
            # Iterate every seller until one carries a usable priced offer;
            # an unpriced first seller is not an absence of the product.
            for seller in item.get("sellers") or []:
                offer = seller.get("commertialOffer") if isinstance(seller, dict) else None
                if isinstance(offer, dict) and offer.get("Price") is not None:
                    return product, item, offer
    if identity_matched:
        # The mapped listing was found but no seller prices it: a source
        # problem, not a not-found result.
        raise ValueError("mapped Dunnes listing has no seller with a priced offer")
    raise LookupError("mapped Dunnes product was not found")


def _validate_listing(name: str, pack: BenchmarkPack) -> str | None:
    """Check that the returned product still matches the Catalog Pack.

    Returns ``None`` when the listing looks like the expected pack,
    or a short reason string when attributes have drifted.
    """
    name_tokens = _normalise_name(name)
    # Validate only the core brand + variant tokens; unit-size tokens may
    # normalise differently (e.g. "330ml" vs "330" + "ml"). Retailer titles
    # may use a known pack alias instead (e.g. "Diet Coke" for a Coca-Cola
    # Diet pack), mirroring matching.name_matches.
    core = _normalise_name(pack.brand) | _normalise_name(pack.variant)
    if not core or core.issubset(name_tokens):
        return None
    if any(
        _normalise_name(alias) and _normalise_name(alias).issubset(name_tokens)
        for alias in pack.aliases
    ):
        return None
    return f"name mismatch: expected {core} not in {name_tokens}"


def ensure_schema(connection: sqlite3.Connection) -> None:
    connection.executescript(SCHEMA)
    connection.execute("PRAGMA foreign_keys = ON")
    # Seed the central retailer lookup table; INSERT OR IGNORE keeps this
    # idempotent and never disturbs operator edits to existing rows.
    connection.executemany(
        """
        INSERT OR IGNORE INTO retailers
            (retailer_slug, display_name, tier, country, data_source_type)
        VALUES (?, ?, ?, 'IE', 'scraper')
        """,
        _RETAILER_SEED,
    )
    # Keep a database created by the first Dunnes-only milestone usable.
    for table, column, definition in (
        ("collection_results", "source_scope", "TEXT"),
        ("collection_results", "complete", "TEXT"),
        ("price_observations", "clubcard_price", "TEXT"),
        ("price_observations", "drs_deposit", "TEXT"),
        ("price_observations", "source_scope", "TEXT"),
    ):
        pragma_cursor = connection.execute(f"PRAGMA table_info({table})")
        try:
            columns = {row[1] for row in pragma_cursor.fetchall()}
        finally:
            pragma_cursor.close()
        if column not in columns:
            connection.execute(f"ALTER TABLE {table} ADD COLUMN {column} {definition}")


_SENSITIVE_KEY = re.compile(r"(?:authorization|cookie|password|secret|token|api.?key)", re.I)


def safe_record(value: Any) -> str | None:
    if value is None:
        return None

    def scrub(item: Any) -> Any:
        if isinstance(item, Mapping):
            return {
                str(key): "[redacted]" if _SENSITIVE_KEY.search(str(key)) else scrub(val)
                for key, val in item.items()
            }
        if isinstance(item, (list, tuple)):
            return [scrub(value) for value in item]
        return item

    try:
        return json.dumps(scrub(value), sort_keys=True, default=str)
    except (TypeError, ValueError):
        return json.dumps(str(value))


def _record_diagnostic(
    database: str | Path,
    run_id: str,
    retailer: str,
    catalog_id: str | None,
    event: str,
    *,
    level: str = "info",
    message: str | None = None,
    raw_record: Any = None,
    request_metadata: Any = None,
) -> None:
    with closing(sqlite3.connect(database)) as connection:
        ensure_schema(connection)
        connection.execute(
            """
            INSERT INTO collection_diagnostics
                (run_id, retailer, catalog_id, level, event, message,
                 raw_record, request_metadata, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                run_id, retailer, catalog_id, level, event, message,
                safe_record(raw_record), safe_record(request_metadata), timestamp(),
            ),
        )
        connection.commit()


def _retrying_fetcher(
    fetcher: Callable[[str], Mapping[str, Any]],
    *,
    database: str | Path,
    run_id: str,
    retailer: str,
    catalog_id: str,
    max_retries: int,
    retry_backoff: float,
    direct_fetcher: Callable[[str], Mapping[str, Any]] | None = None,
) -> Callable[[str], Mapping[str, Any]]:
    def attempt(source_fetcher: Callable[[str], Mapping[str, Any]], query: str) -> Mapping[str, Any]:
        for attempt_number in range(max_retries + 1):
            metadata = {"attempt": attempt_number + 1, "search_term": query}
            _record_diagnostic(
                database, run_id, retailer, catalog_id, "request",
                request_metadata=metadata,
            )
            try:
                payload = source_fetcher(query)
                _record_diagnostic(
                    database, run_id, retailer, catalog_id, "response",
                    raw_record=payload, request_metadata=metadata,
                )
                return payload
            except Exception as exc:
                _record_diagnostic(
                    database, run_id, retailer, catalog_id, "error",
                    level="error", message=str(exc), request_metadata=metadata,
                )
                if attempt_number >= max_retries:
                    raise
                delay = retry_backoff * (2 ** attempt_number)
                _record_diagnostic(
                    database, run_id, retailer, catalog_id, "retry",
                    message=f"retrying after {delay:g}s", request_metadata=metadata,
                )
                if delay:
                    time.sleep(delay)
        raise AssertionError("unreachable")

    def fetch(search_term: str) -> Mapping[str, Any]:
        return attempt(fetcher, search_term)

    if direct_fetcher is not None:
        setattr(fetch, "fetch_product", lambda tpnb: attempt(direct_fetcher, tpnb))
    return fetch


_OBSERVATION_COLUMNS = """
    po.run_id,
    po.catalog_id,
    cp.name AS catalog_name,
    po.retailer,
    po.source_product_reference,
    po.source_item_id,
    po.source_product_name,
    po.displayed_price,
    po.clubcard_price,
    po.drs_deposit,
    po.source_scope,
    po.currency,
    po.pack_count,
    po.unit_size_ml,
    po.package_type,
    po.component_unit_price,
    po.price_per_litre,
    po.observed_at
"""


def _read_rows(
    database: str | Path,
    query: str,
    parameters: tuple[Any, ...] = (),
) -> list[dict[str, Any]]:
    with closing(sqlite3.connect(database)) as connection:
        ensure_schema(connection)
        connection.row_factory = sqlite3.Row
        return [
            dict(row)
            for row in connection.execute(query, parameters).fetchall()
        ]


def _filter_clause(
    retailer: str | None, catalog_id: str | None, prefix: str = ""
) -> tuple[str, tuple[str, ...]]:
    filters: list[str] = []
    parameters: list[str] = []
    if retailer is not None:
        filters.append(f"{prefix}retailer = ?")
        parameters.append(retailer)
    if catalog_id is not None:
        filters.append(f"{prefix}catalog_id = ?")
        parameters.append(catalog_id)
    return (" WHERE " + " AND ".join(filters)) if filters else "", tuple(parameters)


def price_history(
    database: str | Path,
    *,
    retailer: str | None = None,
    catalog_id: str | None = None,
) -> list[dict[str, Any]]:
    """Return append-only Price Observations, newest first."""
    where, parameters = _filter_clause(retailer, catalog_id, "po.")
    filters = where.replace(" WHERE ", " AND ", 1)
    return _read_rows(
        database,
        f"""
        SELECT {_OBSERVATION_COLUMNS}
        FROM price_observations AS po
        LEFT JOIN catalog_packs AS cp ON cp.catalog_id = po.catalog_id
        LEFT JOIN catalog_mappings AS cm
          ON cm.catalog_id = po.catalog_id AND cm.retailer = po.retailer
        WHERE (cm.status IS NULL OR cm.status <> 'dormant'){filters}
        ORDER BY po.observed_at DESC, po.observation_id DESC
        """,
        parameters,
    )


def current_feed(
    database: str | Path,
    *,
    retailer: str | None = None,
    catalog_id: str | None = None,
) -> list[dict[str, Any]]:
    """Return only observed results from the latest result for each pair.

    The latest result wins even when it is ``not_found``, ``source_error``,
    or ``inconclusive``; this prevents an older price from being presented as
    current. Results for other retailer-pack pairs are independent.
    """
    where, parameters = _filter_clause(retailer, catalog_id, "cr.")
    return _read_rows(
        database,
        f"""
        WITH latest_results AS (
            SELECT cr.*,
                   ROW_NUMBER() OVER (
                       PARTITION BY cr.retailer, cr.catalog_id
                       ORDER BY cr.recorded_at DESC, cr.rowid DESC
                   ) AS position
            FROM collection_results AS cr
            {where}
        ),
        latest_observations AS (
            SELECT po.*,
                   ROW_NUMBER() OVER (
                       PARTITION BY po.run_id, po.retailer, po.catalog_id
                       ORDER BY po.observed_at DESC, po.observation_id DESC
                   ) AS obs_position
            FROM price_observations AS po
        )
        SELECT {_OBSERVATION_COLUMNS}
        FROM latest_results AS lr
        JOIN latest_observations AS po
          ON po.run_id = lr.run_id
         AND po.catalog_id = lr.catalog_id
         AND po.retailer = lr.retailer
         AND po.obs_position = 1
        LEFT JOIN catalog_packs AS cp ON cp.catalog_id = po.catalog_id
        WHERE lr.position = 1 AND lr.status = 'observed'
          AND NOT EXISTS (
              SELECT 1 FROM catalog_mappings AS cm
              WHERE cm.catalog_id = po.catalog_id
                AND cm.retailer = po.retailer
                AND cm.status = 'dormant'
          )
        ORDER BY po.retailer, cp.name, po.catalog_id
        """,
        parameters,
    )


def last_seen(
    database: str | Path,
    *,
    retailer: str,
    catalog_id: str,
) -> dict[str, Any] | None:
    """Return the latest successful observation, or ``None`` if never seen.

    A pair that is absent from the Current Feed is reported as
    ``not_seen_since`` rather than being treated as retired.
    """
    observations = price_history(database, retailer=retailer, catalog_id=catalog_id)
    if not observations:
        return None
    observation = observations[0]
    current = any(
        row["retailer"] == retailer and row["catalog_id"] == catalog_id
        for row in current_feed(database, retailer=retailer, catalog_id=catalog_id)
    )
    return observation | {
        "availability": "current" if current else "not_seen_since",
        "not_seen_since": None if current else observation["observed_at"],
    }


def _candidate_products(
    payload: Mapping[str, Any], mapping: DunnesMapping
) -> list[tuple[str, str, str, str | None, str]]:
    products = payload.get("data", {}).get("productSearch", {}).get("products", [])
    candidates = []
    expected = _normalise_name(mapping.expected_product_name)
    for product in products if isinstance(products, list) else []:
        if not isinstance(product, dict):
            continue
        reference = str(product.get("productReference") or "")
        name = str(product.get("productName") or "")
        if mapping.source_product_reference and reference == mapping.source_product_reference:
            continue
        if not mapping.source_product_reference and expected.issubset(_normalise_name(name)):
            continue
        for item in product.get("items") or []:
            item_id = str(item.get("itemId") or "")
            sellers = item.get("sellers") or []
            offer = sellers[0].get("commertialOffer") if sellers else {}
            price = None
            if isinstance(offer, dict) and offer.get("Price") is not None:
                try:
                    price = _decimal_text(_decimal_price(offer["Price"]))
                except ValueError:
                    price = None
            candidates.append((reference, item_id, name, price, safe_record(product) or "{}"))
    return candidates


def collect_one(
    pack: BenchmarkPack,
    mapping: DunnesMapping,
    fetcher: Callable[[str], Mapping[str, Any]],
    database: str | Path,
    *,
    _run_id: str | None = None,
    _started_at: str | None = None,
) -> dict[str, Any]:
    """Collect one mapped pack and return the operator-facing run summary."""
    if pack.catalog_id != mapping.catalog_id:
        raise ValueError("catalog pack and Dunnes mapping must have the same catalog_id")
    if pack.pack_count < 1 or pack.unit_size_ml < 1:
        raise ValueError("pack composition must contain positive count and size")

    started_at = _started_at or timestamp()
    started = time.monotonic()
    run_id = _run_id or uuid.uuid4().hex
    own_run = _run_id is None
    status = "observed"
    error: str | None = None
    product: dict[str, Any] | None = None
    item: dict[str, Any] | None = None
    offer: dict[str, Any] | None = None
    payload: Mapping[str, Any] | None = None
    complete = "unknown"

    if mapping.status != "approved":
        status = "unmapped"
        error = "catalog mapping is not approved"
    else:
        try:
            payload = fetcher(pack.search_term)
            complete = _page_completeness(payload)
            product, item, offer = _find_listing(payload, mapping)
            reason = _validate_listing(product.get("productName", ""), pack)
            if reason is not None:
                status = "source_error"
                error = f"stale source identifier: {reason}"
        except LookupError as exc:
            status = _absence_status(payload)
            error = str(exc)
        except Exception as exc:
            status = "source_error"
            error = str(exc)

    observed_at = timestamp()
    duration_ms = round((time.monotonic() - started) * 1000, 1)
    displayed_price: Decimal | None = None
    drs_deposit: Decimal | None = None
    component_unit_price: str | None = None
    price_per_litre: str | None = None

    if status == "observed":
        assert product is not None and item is not None and offer is not None
        try:
            displayed_price = _decimal_price(offer["Price"])
            drs_deposit = _dunnes_drs_deposit(offer)
            component_unit_price = _decimal_text(displayed_price / pack.pack_count)
            litres = Decimal(pack.pack_count * pack.unit_size_ml) / Decimal(1000)
            price_per_litre = _decimal_text(displayed_price / litres, "0.0001")
        except Exception as exc:
            # Malformed prices demote to source_error; never throw mid-observation.
            status = "source_error"
            error = str(exc)
            displayed_price = None
            drs_deposit = None
            component_unit_price = None
            price_per_litre = None

    summary = {
        "run_id": run_id,
        "retailer": "dunnes",
        "catalog_id": pack.catalog_id,
        "status": status,
        "complete": complete,
        "observed_count": int(status == "observed"),
        "failed_count": int(status == "source_error"),
        "duration_ms": duration_ms,
    }

    database_path = Path(database)
    database_path.parent.mkdir(parents=True, exist_ok=True)
    with closing(sqlite3.connect(database_path)) as connection:
        ensure_schema(connection)
        connection.execute(
            """
            INSERT INTO catalog_packs VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(catalog_id) DO UPDATE SET
                name=excluded.name, brand=excluded.brand, variant=excluded.variant,
                pack_count=excluded.pack_count, unit_size_ml=excluded.unit_size_ml,
                package_type=excluded.package_type, search_term=excluded.search_term
            """,
            (
                pack.catalog_id,
                pack.name,
                pack.brand,
                pack.variant,
                pack.pack_count,
                pack.unit_size_ml,
                pack.package_type,
                pack.search_term,
            ),
        )
        connection.execute(
            """
            INSERT INTO catalog_mappings
                (catalog_id, retailer, expected_product_name,
                 source_product_reference, source_item_id, status)
            VALUES (?, 'dunnes', ?, ?, ?, ?)
            ON CONFLICT(catalog_id, retailer) DO UPDATE SET
                expected_product_name=excluded.expected_product_name,
                source_product_reference=excluded.source_product_reference,
                source_item_id=excluded.source_item_id,
                status=excluded.status
            """,
            (
                mapping.catalog_id,
                mapping.expected_product_name,
                mapping.source_product_reference,
                mapping.source_item_id,
                mapping.status,
            ),
        )
        if own_run:
            connection.execute(
                "INSERT INTO collection_runs VALUES (?, ?, ?, ?, ?, ?, ?)",
                (
                    run_id,
                    started_at,
                    observed_at,
                    "completed" if status != "source_error" else "failed",
                    summary["observed_count"],
                    summary["failed_count"],
                    json.dumps(summary),
                ),
            )
        connection.execute(
            """
            INSERT INTO collection_results (
                run_id, catalog_id, retailer, status, error,
                source_product_reference, source_item_id, source_scope,
                complete, recorded_at
            ) VALUES (?, ?, 'dunnes', ?, ?, ?, ?, NULL, ?, ?)
            """,
            (
                run_id,
                pack.catalog_id,
                status,
                error,
                product.get("productReference") if product else None,
                item.get("itemId") if item else None,
                complete,
                observed_at,
            ),
        )
        if payload is not None:
            for reference, item_id, name, price, raw_record in _candidate_products(payload, mapping):
                candidate_id = f"dunnes:{reference}:{item_id}"
                connection.execute(
                    """
                    INSERT INTO catalog_candidates (
                        candidate_id, retailer, source_product_reference, source_item_id,
                        source_product_name, displayed_price, raw_record, status, first_seen_at
                    ) VALUES (?, 'dunnes', ?, ?, ?, ?, ?, 'pending_review', ?)
                    ON CONFLICT(candidate_id) DO UPDATE SET
                        displayed_price=excluded.displayed_price,
                        raw_record=excluded.raw_record
                    """,
                    (candidate_id, reference, item_id, name, price, raw_record, observed_at),
                )
        if status == "observed":
            assert product is not None and item is not None and displayed_price is not None
            connection.execute(
                """
                INSERT INTO price_observations (
                    run_id, catalog_id, retailer, source_product_reference,
                    source_item_id, source_product_name, displayed_price, drs_deposit,
                    currency, pack_count, unit_size_ml, package_type,
                    component_unit_price, price_per_litre, observed_at
                ) VALUES (?, ?, 'dunnes', ?, ?, ?, ?, ?, 'EUR', ?, ?, ?, ?, ?, ?)
                """,
                (
                    run_id,
                    pack.catalog_id,
                    product.get("productReference", ""),
                    item.get("itemId", ""),
                    product.get("productName", ""),
                    _decimal_text(displayed_price),
                    _decimal_text(drs_deposit) if drs_deposit is not None else None,
                    pack.pack_count,
                    pack.unit_size_ml,
                    pack.package_type,
                    component_unit_price,
                    price_per_litre,
                    observed_at,
                ),
            )
            if drs_deposit is None:
                # Validated source limitation: no live VTEX offer carries
                # deposit evidence (see _dunnes_drs_deposit).
                connection.execute(
                    """
                    INSERT INTO collection_diagnostics
                        (run_id, retailer, catalog_id, level, event, message,
                         raw_record, request_metadata, created_at)
                    VALUES (?, 'dunnes', ?, 'warning', 'drs_not_available', ?, NULL, NULL, ?)
                    """,
                    (
                        run_id,
                        pack.catalog_id,
                        "Dunnes VTEX offer evidence exposes no DRS deposit;"
                        " stored as NULL",
                        timestamp(),
                    ),
                )
        connection.commit()
    return summary | ({"error": error} if error else {})


def collect_catalog(
    catalog: list[BenchmarkPack],
    mappings: list[DunnesMapping],
    fetcher: Callable[[str], Mapping[str, Any]],
    database: str | Path,
) -> list[dict[str, Any]]:
    """Run only approved retailer mappings; leave review/unmapped packs untouched."""
    mappings_by_pack = {
        mapping.catalog_id: mapping
        for mapping in mappings
        if mapping.status == "approved"
    }
    return [
        collect_one(pack, mappings_by_pack[pack.catalog_id], fetcher, database)
        for pack in catalog
        if pack.catalog_id in mappings_by_pack
    ]


SUPERVALU_HOME = "https://shop.supervalu.ie/"
SUPERVALU_ENDPOINT = "https://storefrontgateway.supervalu.ie/api/stores/{store_id}/search"
SUPERVALU_PRODUCT_ENDPOINT = "https://storefrontgateway.supervalu.ie/api/stores/{store_id}/products/{product_id}"


class SuperValuClient:
    """Fetch SuperValu's store-scoped search JSON with a storefront cookie."""

    def __init__(
        self,
        store_id: str,
        endpoint: str = SUPERVALU_ENDPOINT,
        opener: urllib.request.OpenerDirector | None = None,
        product_endpoint: str = SUPERVALU_PRODUCT_ENDPOINT,
    ):
        if not store_id.strip():
            raise ValueError("SuperValu store_id must not be empty")
        self.store_id = store_id
        self.endpoint = endpoint
        self.product_endpoint = product_endpoint
        self.opener = opener or urllib.request.build_opener(
            urllib.request.HTTPCookieProcessor(http.cookiejar.CookieJar())
        )
        self._session_ready = False

    def __call__(self, search_term: str) -> dict[str, Any]:
        if not self._session_ready:
            self._get(SUPERVALU_HOME, parse_json=False)
            self._session_ready = True
        url = self.endpoint.format(store_id=urllib.parse.quote(self.store_id, safe=""))
        url += "?" + urllib.parse.urlencode({"q": search_term, "take": 50})
        payload = self._get(url)
        if not isinstance(payload, dict):
            raise RuntimeError("SuperValu response was not a JSON object")
        return payload

    def fetch_product(self, product_id: str) -> dict[str, Any]:
        """Hydrate one known product ID without repeating a catalog search."""
        if not str(product_id).strip():
            raise ValueError("SuperValu product_id must not be empty")
        url = self.product_endpoint.format(
            store_id=urllib.parse.quote(self.store_id, safe=""),
            product_id=urllib.parse.quote(str(product_id), safe=""),
        )
        payload = self._get(url)
        if not isinstance(payload, dict):
            raise RuntimeError("SuperValu product response was not a JSON object")
        # The detail endpoint keys identity by ``sku``; downstream hydration
        # checks ``productId``. Normalize, and mirror the formatted price into
        # ``priceNumeric`` so numeric-first readers see it.
        if not payload.get("productId") and payload.get("sku"):
            payload["productId"] = payload["sku"]
        if payload.get("priceNumeric") is None and payload.get("price") is not None:
            try:
                from decimal import Decimal as _D

                payload["priceNumeric"] = float(
                    str(payload["price"]).replace("€", "").strip()
                )
            except (ValueError, TypeError):
                pass
        return payload

    def _get(self, url: str, *, parse_json: bool = True) -> Any:
        request = urllib.request.Request(
            url,
            headers={
                "Accept": "application/json",
                "User-Agent": "drinks-tracker/0.1",
                "Origin": "https://shop.supervalu.ie",
                "Referer": SUPERVALU_HOME,
            },
        )
        try:
            with self.opener.open(request, timeout=30) as response:
                if getattr(response, "status", 200) >= 400:
                    raise RuntimeError(f"SuperValu HTTP {response.status}")
                return json.load(response) if parse_json else response.read()
        except Exception as exc:
            if isinstance(exc, RuntimeError):
                raise
            raise RuntimeError(f"SuperValu request failed: {exc}") from exc


def _find_supervalu_listing(
    payload: Mapping[str, Any], mapping: SuperValuMapping
) -> dict[str, Any]:
    items = payload.get("items")
    if not isinstance(items, list):
        raise ValueError("SuperValu response has no items list")
    expected_tokens = _normalise_name(mapping.expected_product_name)
    for item in items:
        if not isinstance(item, dict):
            continue
        product_id = str(item.get("productId") or item.get("sku") or "")
        if mapping.source_product_id and product_id != str(mapping.source_product_id):
            continue
        if not mapping.source_product_id and not expected_tokens.issubset(
            _normalise_name(str(item.get("name") or ""))
        ):
            continue
        return item
    raise LookupError("mapped SuperValu product was not found")


def _price_value(value: Any) -> Any:
    if isinstance(value, dict):
        for key in ("priceNumeric", "amount", "value", "price"):
            if value.get(key) is not None:
                return value[key]
    return value


def _optional_price(item: Mapping[str, Any], *keys: str) -> Decimal | None:
    for key in keys:
        if item.get(key) is not None:
            return _decimal_price(_price_value(item[key]))
    return None


def _supervalu_drs_deposit(item: Mapping[str, Any]) -> Decimal | None:
    tax_details = item.get("taxDetails") or []
    if isinstance(tax_details, list):
        for detail in tax_details:
            if isinstance(detail, dict) and str(detail.get("groupName", "")).lower() == "deposit":
                return _decimal_price(detail.get("amount"))
    return _optional_price(item, "drsDeposit", "deposit", "depositAmount")


def collect_supervalu_one(
    pack: BenchmarkPack,
    mapping: SuperValuMapping,
    fetcher: Callable[[str], Mapping[str, Any]],
    database: str | Path,
    *,
    store_id: str | None = None,
    hydrator: Callable[[str], Mapping[str, Any]] | None = None,
    _run_id: str | None = None,
    _started_at: str | None = None,
) -> dict[str, Any]:
    """Collect one mapped pack from one configured SuperValu store.

    When *mapping.source_product_id* is known, uses direct product-ID
    hydration (via *hydrator* or ``fetcher.fetch_product``) instead of
    repeating the catalog search.
    """
    if pack.catalog_id != mapping.catalog_id:
        raise ValueError("catalog pack and SuperValu mapping must have the same catalog_id")
    if pack.pack_count < 1 or pack.unit_size_ml < 1:
        raise ValueError("pack composition must contain positive count and size")
    store_id = store_id or getattr(fetcher, "store_id", None)
    if not store_id:
        raise ValueError("SuperValu store_id is required")

    started_at = _started_at or timestamp()
    started = time.monotonic()
    run_id = _run_id or uuid.uuid4().hex
    own_run = _run_id is None
    status = "observed"
    error: str | None = None
    item: dict[str, Any] | None = None
    payload: Mapping[str, Any] | None = None
    complete = "unknown"

    if mapping.status != "approved":
        status = "unmapped"
        error = "catalog mapping is not approved"
    else:
        try:
            # Direct hydration via stable source identifier where available.
            if mapping.source_product_id:
                hydrate = hydrator or getattr(fetcher, "fetch_product", None)
                if hydrate is not None and callable(hydrate):
                    payload = hydrate(str(mapping.source_product_id))
                    complete = _page_completeness(payload)
                    items = payload.get("items")
                    if isinstance(items, list) and items:
                        item = items[0] if isinstance(items[0], dict) else None
                    elif isinstance(payload, dict) and payload.get("productId"):
                        item = payload
                    if item is None:
                        raise LookupError(f"SuperValu product {mapping.source_product_id} returned no item")
                else:
                    payload = fetcher(pack.search_term)
                    complete = _page_completeness(payload)
                    item = _find_supervalu_listing(payload, mapping)
            else:
                payload = fetcher(pack.search_term)
                complete = _page_completeness(payload)
                item = _find_supervalu_listing(payload, mapping)
            reason = _validate_listing(item.get("name", ""), pack)
            if reason is not None:
                status = "source_error"
                error = f"stale source identifier: {reason}"
        except LookupError as exc:
            status = _absence_status(payload)
            error = str(exc)
        except Exception as exc:
            status = "source_error"
            error = str(exc)

    observed_at = timestamp()
    duration_ms = round((time.monotonic() - started) * 1000, 1)
    displayed_price: Decimal | None = None
    clubcard_price: Decimal | None = None
    drs_deposit: Decimal | None = None
    component_unit_price: str | None = None
    price_per_litre: str | None = None

    if status == "observed":
        assert item is not None
        try:
            displayed_price = _decimal_price(
                item.get("priceNumeric")
                if item.get("priceNumeric") is not None
                else item.get("price")
            )
            clubcard_price = _optional_price(
                item, "clubcardPrice", "clubCardPrice", "loyaltyPrice", "memberPrice"
            )
            drs_deposit = _supervalu_drs_deposit(item)
            component_unit_price = _decimal_text(displayed_price / pack.pack_count)
            litres = Decimal(pack.pack_count * pack.unit_size_ml) / Decimal(1000)
            price_per_litre = _decimal_text(displayed_price / litres, "0.0001")
        except Exception as exc:
            status = "source_error"
            error = str(exc)
            displayed_price = None
            clubcard_price = None
            drs_deposit = None
            component_unit_price = None
            price_per_litre = None

    source_product_id = (
        str(item.get("productId") or item.get("sku") or "")
        if item
        else str(mapping.source_product_id or "")
    )
    source_item_id = (
        str(item.get("sku") or item.get("productId") or "") if item else ""
    )
    summary = {
        "run_id": run_id,
        "retailer": "supervalu",
        "catalog_id": pack.catalog_id,
        "source_scope": store_id,
        "status": status,
        "complete": complete,
        "observed_count": int(status == "observed"),
        "failed_count": int(status == "source_error"),
        "duration_ms": duration_ms,
    }

    database_path = Path(database)
    database_path.parent.mkdir(parents=True, exist_ok=True)
    with closing(sqlite3.connect(database_path)) as connection:
        ensure_schema(connection)
        connection.execute(
            """
            INSERT INTO catalog_packs VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(catalog_id) DO UPDATE SET
                name=excluded.name, brand=excluded.brand, variant=excluded.variant,
                pack_count=excluded.pack_count, unit_size_ml=excluded.unit_size_ml,
                package_type=excluded.package_type, search_term=excluded.search_term
            """,
            (
                pack.catalog_id, pack.name, pack.brand, pack.variant, pack.pack_count,
                pack.unit_size_ml, pack.package_type, pack.search_term,
            ),
        )
        connection.execute(
            """
            INSERT INTO catalog_mappings
                (catalog_id, retailer, expected_product_name,
                 source_product_reference, source_item_id, status)
            VALUES (?, 'supervalu', ?, ?, ?, ?)
            ON CONFLICT(catalog_id, retailer) DO UPDATE SET
                expected_product_name=excluded.expected_product_name,
                source_product_reference=excluded.source_product_reference,
                source_item_id=excluded.source_item_id, status=excluded.status
            """,
            (
                mapping.catalog_id,
                mapping.expected_product_name,
                source_product_id or mapping.source_product_id,
                source_item_id or None,
                mapping.status,
            ),
        )
        if own_run:
            connection.execute(
                """
                INSERT INTO collection_runs
                    (run_id, started_at, finished_at, status, observed_count, failed_count, summary)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    run_id, started_at, observed_at,
                    "completed" if status != "source_error" else "failed",
                    summary["observed_count"], summary["failed_count"], json.dumps(summary),
                ),
            )
        connection.execute(
            """
            INSERT INTO collection_results
                (run_id, catalog_id, retailer, status, error,
                 source_product_reference, source_item_id, source_scope,
                 complete, recorded_at)
            VALUES (?, ?, 'supervalu', ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                run_id, pack.catalog_id, status, error,
                source_product_id or None,
                source_item_id or None,
                store_id,
                complete,
                observed_at,
            ),
        )
        if status == "observed":
            assert item is not None and displayed_price is not None
            connection.execute(
                """
                INSERT INTO price_observations (
                    run_id, catalog_id, retailer, source_product_reference,
                    source_item_id, source_product_name, displayed_price, clubcard_price,
                    drs_deposit, source_scope, currency, pack_count, unit_size_ml,
                    package_type, component_unit_price, price_per_litre, observed_at
                ) VALUES (?, ?, 'supervalu', ?, ?, ?, ?, ?, ?, ?, 'EUR', ?, ?, ?, ?, ?, ?)
                """,
                (
                    run_id, pack.catalog_id, source_product_id,
                    source_item_id, item.get("name", ""),
                    _decimal_text(displayed_price),
                    _decimal_text(clubcard_price) if clubcard_price is not None else None,
                    _decimal_text(drs_deposit) if drs_deposit is not None else None,
                    store_id, pack.pack_count, pack.unit_size_ml, pack.package_type,
                    component_unit_price, price_per_litre, observed_at,
                ),
            )
        connection.commit()
    return summary | ({"error": error} if error else {})


TESCO_SEARCH_ENDPOINT = "https://search.api.tesco.com/search"
TESCO_GRAPHQL_ENDPOINT = "https://xapi.tesco.com/"
TESCO_PRODUCT_QUERY = """
query GetProductByTpnb($tpnb: String) {
  product(tpnb: $tpnb) {
    id
    gtin
    title
    price { actual unitPrice unitOfMeasure }
    details { packSize { value units } }
    promotions { description attributes }
    charges { ... on ProductDepositReturnCharge { amount } }
  }
}
"""


class TescoClient:
    """Fetch Irish Tesco search results and hydrate them through GraphQL.

    Tesco's GraphQL gateway sits behind Akamai TLS fingerprinting: plain
    urllib gets 403'd even with correct headers (validated live). When no
    explicit ``opener`` is injected and ``curl-cffi`` is installed, requests
    go through a Chrome-impersonated session instead — the only transport
    observed working reliably. Tests inject an opener and always take the
    plain-urllib path.
    """

    def __init__(
        self,
        api_key: str | None = None,
        search_endpoint: str = TESCO_SEARCH_ENDPOINT,
        graphql_endpoint: str = TESCO_GRAPHQL_ENDPOINT,
        opener: urllib.request.OpenerDirector | None = None,
        min_request_interval: float = 1.0,
    ):
        self.api_key = api_key or os.environ.get("TESCO_API_KEY")
        if not self.api_key:
            raise ValueError("Tesco API key is required; set TESCO_API_KEY")
        if min_request_interval < 0:
            raise ValueError("Tesco request interval must not be negative")
        self.search_endpoint = search_endpoint
        self.graphql_endpoint = graphql_endpoint
        self.opener = opener or urllib.request.build_opener()
        self.min_request_interval = min_request_interval
        self._last_request_at: float | None = None
        self._impersonator: Any | None = None
        if opener is None:
            try:
                from curl_cffi import requests as curl_requests
            except ImportError:
                return
            self._impersonator = curl_requests.Session(impersonate="chrome")

    def __call__(self, search_term: str) -> dict[str, Any]:
        if not search_term.strip():
            raise ValueError("Tesco search term must not be empty")
        search_url = self.search_endpoint + "?" + urllib.parse.urlencode(
            {"distchannel": "ghs", "query": search_term, "count": 10, "geo": "ie"}
        )
        search_payload = self._request_json(
            urllib.request.Request(
                search_url,
                headers={
                    "Accept": "application/json",
                    "User-Agent": "drinks-tracker/0.1",
                    "Accept-Language": "en-IE,en;q=0.9",
                },
            )
        )
        try:
            results = search_payload["ie"]["ghs"]["products"]["results"]
            tpnbs = [str(result["tpnb"]) for result in results if result.get("tpnb")]
        except (KeyError, TypeError, AttributeError) as exc:
            raise RuntimeError("Tesco search response has no product results") from exc
        if not isinstance(results, list):
            raise RuntimeError("Tesco search response has no product results")
        if not tpnbs:
            return {"products": [], "search": search_payload}
        return {"products": self._hydrate_tpnbs(tpnbs), "search": search_payload}

    def fetch_product(self, tpnb: str) -> dict[str, Any]:
        """Hydrate one known Tesco product without repeating product search."""
        if not str(tpnb).strip():
            raise ValueError("Tesco TPNB must not be empty")
        return {"products": self._hydrate_tpnbs([str(tpnb)])}

    def _hydrate_tpnbs(self, tpnbs: list[str]) -> list[dict[str, Any]]:
        batch = [
            {
                "operationName": "GetProductByTpnb",
                "variables": {"tpnb": tpnb},
                "query": TESCO_PRODUCT_QUERY,
            }
            for tpnb in tpnbs
        ]
        detail_payload = self._request_json(
            urllib.request.Request(
                self.graphql_endpoint,
                data=json.dumps(batch).encode(),
                headers={
                    "Accept": "application/json",
                    "Content-Type": "application/json",
                    "User-Agent": "drinks-tracker/0.1",
                    "x-apikey": self.api_key or "",
                    "region": "IE",
                    "language": "en-IE",
                    "origin": "https://www.tesco.ie",
                    "referer": "https://www.tesco.ie/",
                },
                method="POST",
            )
        )
        if not isinstance(detail_payload, list):
            raise RuntimeError("Tesco GraphQL response was not a list")
        products: list[dict[str, Any]] = []
        for tpnb, result in zip(tpnbs, detail_payload):
            if not isinstance(result, dict):
                raise RuntimeError("Tesco GraphQL response contained an invalid result")
            if result.get("errors"):
                message = result["errors"][0].get("message", "GraphQL error")
                raise RuntimeError(f"Tesco GraphQL error: {message}")
            product = result.get("data", {}).get("product")
            if isinstance(product, dict):
                products.append({"tpnb": tpnb, **product})
        return products

    def _request_json(self, request: urllib.request.Request) -> Any:
        if self._last_request_at is not None:
            delay = self.min_request_interval - (time.monotonic() - self._last_request_at)
            if delay > 0:
                time.sleep(delay)
        try:
            if self._impersonator is not None:
                response = self._impersonator.request(
                    request.get_method(),
                    request.full_url,
                    headers=dict(request.header_items()),
                    data=request.data,
                    timeout=30,
                )
                if response.status_code >= 400:
                    raise RuntimeError(f"Tesco HTTP {response.status_code}")
                return response.json()
            with self.opener.open(request, timeout=30) as response:
                if getattr(response, "status", 200) >= 400:
                    raise RuntimeError(f"Tesco HTTP {response.status}")
                return json.load(response)
        except Exception as exc:
            if isinstance(exc, RuntimeError):
                raise
            raise RuntimeError(f"Tesco request failed: {exc}") from exc
        finally:
            self._last_request_at = time.monotonic()


def _find_tesco_listing(
    payload: Mapping[str, Any], mapping: TescoMapping
) -> dict[str, Any]:
    products = payload.get("products")
    if not isinstance(products, list):
        raise ValueError("Tesco response has no products list")
    expected_tokens = _normalise_name(mapping.expected_product_name)
    for product in products:
        if not isinstance(product, dict):
            continue
        tpnb = str(product.get("tpnb") or "")
        if mapping.source_tpnb and tpnb != str(mapping.source_tpnb):
            continue
        if not mapping.source_tpnb and not expected_tokens.issubset(
            _normalise_name(str(product.get("title") or ""))
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
            total = _decimal_price(multi.group(2).replace(",", "."))
            if quantity > 0 and total is not None:
                return (total / quantity).quantize(Decimal("0.01"), ROUND_HALF_UP)
            continue
        if multi:
            continue
        if re.search(r"club\s*card\s*price", description, re.IGNORECASE):
            match = re.search(r"€\s*([0-9]+(?:[.,][0-9]+)?)", description)
            if match:
                return _decimal_price(match.group(1).replace(",", "."))
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
                amount = _decimal_price(charge["amount"])
                if amount is not None:
                    return amount
    details = item.get("details")
    if isinstance(details, dict):
        tax_details = details.get("taxDetails") or details.get("taxes") or []
        if isinstance(tax_details, list):
            for detail in tax_details:
                if isinstance(detail, dict) and str(detail.get("groupName", "")).lower() == "deposit":
                    return _decimal_price(detail.get("amount"))
        return _optional_price(details, "drsDeposit", "deposit", "depositAmount")
    return _optional_price(item, "drsDeposit", "deposit", "depositAmount")


def collect_tesco_one(
    pack: BenchmarkPack,
    mapping: TescoMapping,
    fetcher: Callable[[str], Mapping[str, Any]],
    database: str | Path,
    *,
    _run_id: str | None = None,
    _started_at: str | None = None,
) -> dict[str, Any]:
    """Collect one mapped pack from Tesco Ireland's public API."""
    if pack.catalog_id != mapping.catalog_id:
        raise ValueError("catalog pack and Tesco mapping must have the same catalog_id")
    if pack.pack_count < 1 or pack.unit_size_ml < 1:
        raise ValueError("pack composition must contain positive count and size")

    started_at = _started_at or timestamp()
    started = time.monotonic()
    run_id = _run_id or uuid.uuid4().hex
    own_run = _run_id is None
    status = "observed"
    error: str | None = None
    item: dict[str, Any] | None = None
    payload: Mapping[str, Any] | None = None
    complete = "unknown"
    if mapping.status != "approved":
        status = "unmapped"
        error = "catalog mapping is not approved"
    else:
        fallback_diagnostic = False
        try:
            direct_fetcher = getattr(fetcher, "fetch_product", None)
            if mapping.source_tpnb and not callable(direct_fetcher):
                # Fixture case: direct TPNB hydration was expected for this
                # mapping, so the search fallback is recorded once the run row
                # exists (after the persistence block below).
                fallback_diagnostic = True
            payload = (
                direct_fetcher(mapping.source_tpnb)
                if mapping.source_tpnb and callable(direct_fetcher)
                else fetcher(pack.search_term)
            )
            complete = _page_completeness(payload)
            item = _find_tesco_listing(payload, mapping)
            reason = _validate_listing(item.get("title", ""), pack)
            if reason is not None:
                status = "source_error"
                error = f"stale source identifier: {reason}"
        except LookupError as exc:
            status = _absence_status(payload)
            error = str(exc)
        except Exception as exc:
            status = "source_error"
            error = str(exc)

    displayed_price: Decimal | None = None
    clubcard_price: Decimal | None = None
    drs_deposit: Decimal | None = None
    component_unit_price: str | None = None
    price_per_litre: str | None = None
    if status == "observed":
        assert item is not None
        try:
            price = item.get("price") or {}
            displayed_price = _decimal_price(price.get("actual"))
            clubcard_price = _tesco_clubcard_price(item)
            drs_deposit = _tesco_drs_deposit(item)
            component_unit_price = _decimal_text(displayed_price / pack.pack_count)
            litres = Decimal(pack.pack_count * pack.unit_size_ml) / Decimal(1000)
            price_per_litre = _decimal_text(displayed_price / litres, "0.0001")
        except Exception as exc:
            status = "source_error"
            error = str(exc)

    source_product_reference = str(item.get("tpnb") or "") if item else str(mapping.source_tpnb or "")
    source_item_id = (
        str(item.get("id") or item.get("gtin") or item.get("tpnb") or "") if item else ""
    )
    summary = {
        "run_id": run_id,
        "retailer": "tesco",
        "catalog_id": pack.catalog_id,
        "status": status,
        "complete": complete,
        "observed_count": int(status == "observed"),
        "failed_count": int(status == "source_error"),
        "duration_ms": round((time.monotonic() - started) * 1000, 1),
    }

    database_path = Path(database)
    database_path.parent.mkdir(parents=True, exist_ok=True)
    with closing(sqlite3.connect(database_path)) as connection:
        ensure_schema(connection)
        connection.execute(
            """
            INSERT INTO catalog_packs VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(catalog_id) DO UPDATE SET
                name=excluded.name, brand=excluded.brand, variant=excluded.variant,
                pack_count=excluded.pack_count, unit_size_ml=excluded.unit_size_ml,
                package_type=excluded.package_type, search_term=excluded.search_term
            """,
            (pack.catalog_id, pack.name, pack.brand, pack.variant, pack.pack_count,
             pack.unit_size_ml, pack.package_type, pack.search_term),
        )
        connection.execute(
            """
            INSERT INTO catalog_mappings
                (catalog_id, retailer, expected_product_name,
                 source_product_reference, source_item_id, status)
            VALUES (?, 'tesco', ?, ?, ?, ?)
            ON CONFLICT(catalog_id, retailer) DO UPDATE SET
                expected_product_name=excluded.expected_product_name,
                source_product_reference=excluded.source_product_reference,
                source_item_id=excluded.source_item_id, status=excluded.status
            """,
            (mapping.catalog_id, mapping.expected_product_name,
             source_product_reference or mapping.source_tpnb, source_item_id or None,
             mapping.status),
        )
        if own_run:
            connection.execute(
                """
                INSERT INTO collection_runs
                    (run_id, started_at, finished_at, status, observed_count, failed_count, summary)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (run_id, started_at, timestamp(),
                 "completed" if status != "source_error" else "failed",
                 summary["observed_count"], summary["failed_count"], json.dumps(summary)),
            )
        connection.execute(
            """
            INSERT INTO collection_results
                (run_id, catalog_id, retailer, status, error,
                 source_product_reference, source_item_id, source_scope,
                 complete, recorded_at)
            VALUES (?, ?, 'tesco', ?, ?, ?, ?, NULL, ?, ?)
            """,
            (run_id, pack.catalog_id, status, error,
             source_product_reference or None, source_item_id or None,
             complete, timestamp()),
        )
        if status == "observed":
            assert item is not None and displayed_price is not None
            connection.execute(
                """
                INSERT INTO price_observations (
                    run_id, catalog_id, retailer, source_product_reference,
                    source_item_id, source_product_name, displayed_price, clubcard_price,
                    drs_deposit, source_scope, currency, pack_count, unit_size_ml,
                    package_type, component_unit_price, price_per_litre, observed_at
                ) VALUES (?, ?, 'tesco', ?, ?, ?, ?, ?, ?, NULL, 'EUR', ?, ?, ?, ?, ?, ?)
                """,
                (run_id, pack.catalog_id, source_product_reference, source_item_id,
                 item.get("title", ""), _decimal_text(displayed_price),
                 _decimal_text(clubcard_price) if clubcard_price is not None else None,
                 _decimal_text(drs_deposit) if drs_deposit is not None else None,
                 pack.pack_count, pack.unit_size_ml, pack.package_type,
                 component_unit_price, price_per_litre, timestamp()),
            )
        if fallback_diagnostic:
            connection.execute(
                """
                INSERT INTO collection_diagnostics
                    (run_id, retailer, catalog_id, level, event, message,
                     raw_record, request_metadata, created_at)
                VALUES (?, 'tesco', ?, 'warning', 'collection_fallback', ?, NULL, NULL, ?)
                """,
                (run_id, pack.catalog_id,
                 "direct TPNB hydration expected; falling back to search", timestamp()),
            )
        connection.commit()
    return summary | ({"error": error} if error else {})


LIDL_SEARCH_ENDPOINT = "https://www.lidl.ie/q/api/search"
LIDL_BASE_URL = "https://www.lidl.ie"
LIDL_SEARCH_ACCEPT = "application/mindshift.search+json"
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


class LidlClient:
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
        base_url: str = LIDL_BASE_URL,
        opener: urllib.request.OpenerDirector | None = None,
        min_request_interval: float = 1.0,
    ):
        if min_request_interval < 0:
            raise ValueError("Lidl request interval must not be negative")
        self.endpoint = endpoint
        self.base_url = base_url.rstrip("/")
        self.opener = opener or urllib.request.build_opener()
        self.min_request_interval = min_request_interval
        self._last_request_at: float | None = None

    def __call__(self, search_term: str) -> dict[str, Any]:
        """Search Lidl Ireland products."""
        if not search_term.strip():
            raise ValueError("Lidl search term must not be empty")
        payload = self._request_json(
            self.endpoint + "?" + urllib.parse.urlencode({
                "assortment": "IE",
                "locale": "en_IE",
                "q": search_term,
                "version": "2.1.1",
                "fetchsize": "100",
            })
        )
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
        redirect = self._request_json(
            self.endpoint + "?" + urllib.parse.urlencode({
                "assortment": "IE",
                "locale": "en_IE",
                "q": identifier,
                "version": "2.1.1",
            })
        )
        if not isinstance(redirect, dict):
            raise RuntimeError("Lidl product lookup was not a JSON object")
        path = redirect.get("redirectURL")
        if not isinstance(path, str) or not path:
            return {"items": []}
        html = self._request_text(self.base_url + path)
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

    def _throttle(self) -> None:
        if self._last_request_at is not None:
            delay = self.min_request_interval - (time.monotonic() - self._last_request_at)
            if delay > 0:
                time.sleep(delay)

    def _request_json(self, url: str) -> Any:
        return json.loads(self._request_text(url, accept=LIDL_SEARCH_ACCEPT))

    def _request_text(self, url: str, *, accept: str = "text/html") -> str:
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
            return body.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise RuntimeError(f"Lidl response was not UTF-8: {exc}") from exc


def _find_lidl_listing(
    payload: Mapping[str, Any], mapping: LidlMapping
) -> dict[str, Any]:
    items = payload.get("items")
    if not isinstance(items, list):
        raise ValueError("Lidl response has no items list")
    expected_tokens = _normalise_name(mapping.expected_product_name)
    for item in items:
        if not isinstance(item, dict):
            continue
        product_id = str(item.get("productId") or "")
        if mapping.source_product_id and product_id != str(mapping.source_product_id):
            continue
        if not mapping.source_product_id and not expected_tokens.issubset(
            _normalise_name(str(item.get("name") or ""))
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
            return _decimal_price(amount)
    text = item.get("basePriceText")
    if text is not None and re.search(r"deposit", str(text), re.IGNORECASE):
        match = re.search(r"€\s*([0-9]+(?:[.,][0-9]+)?)", str(text))
        if match:
            return _decimal_price(match.group(1).replace(",", "."))
    return None


def collect_lidl_one(
    pack: BenchmarkPack,
    mapping: LidlMapping,
    fetcher: Callable[[str], Mapping[str, Any]],
    database: str | Path,
    *,
    _run_id: str | None = None,
    _started_at: str | None = None,
) -> dict[str, Any]:
    """Collect one mapped pack from Lidl Ireland's storefront."""
    if pack.catalog_id != mapping.catalog_id:
        raise ValueError("catalog pack and Lidl mapping must have the same catalog_id")
    if pack.pack_count < 1 or pack.unit_size_ml < 1:
        raise ValueError("pack composition must contain positive count and size")

    started_at = _started_at or timestamp()
    started = time.monotonic()
    run_id = _run_id or uuid.uuid4().hex
    own_run = _run_id is None
    status = "observed"
    error: str | None = None
    item: dict[str, Any] | None = None
    payload: Mapping[str, Any] | None = None
    complete = "unknown"
    if mapping.status != "approved":
        status = "unmapped"
        error = "catalog mapping is not approved"
    else:
        try:
            direct_fetcher = getattr(fetcher, "fetch_product", None)
            payload = (
                direct_fetcher(str(mapping.source_product_id))
                if mapping.source_product_id and callable(direct_fetcher)
                else fetcher(pack.search_term)
            )
            complete = _page_completeness(payload)
            item = _find_lidl_listing(payload, mapping)
            reason = _validate_listing(item.get("name", ""), pack)
            if reason is not None:
                status = "source_error"
                error = f"stale source identifier: {reason}"
        except LookupError as exc:
            status = _absence_status(payload)
            error = str(exc)
        except Exception as exc:
            status = "source_error"
            error = str(exc)

    displayed_price: Decimal | None = None
    drs_deposit: Decimal | None = None
    component_unit_price: str | None = None
    price_per_litre: str | None = None
    if status == "observed":
        assert item is not None
        try:
            displayed_price = _decimal_price(item.get("price"))
            drs_deposit = _lidl_drs_deposit(item)
            component_unit_price = _decimal_text(displayed_price / pack.pack_count)
            litres = Decimal(pack.pack_count * pack.unit_size_ml) / Decimal(1000)
            price_per_litre = _decimal_text(displayed_price / litres, "0.0001")
        except Exception as exc:
            status = "source_error"
            error = str(exc)

    source_product_id = (
        str(item.get("productId") or "") if item else str(mapping.source_product_id or "")
    )
    summary = {
        "run_id": run_id,
        "retailer": "lidl",
        "catalog_id": pack.catalog_id,
        "status": status,
        "complete": complete,
        "observed_count": int(status == "observed"),
        "failed_count": int(status == "source_error"),
        "duration_ms": round((time.monotonic() - started) * 1000, 1),
    }

    database_path = Path(database)
    database_path.parent.mkdir(parents=True, exist_ok=True)
    with closing(sqlite3.connect(database_path)) as connection:
        ensure_schema(connection)
        connection.execute(
            """
            INSERT INTO catalog_packs VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(catalog_id) DO UPDATE SET
                name=excluded.name, brand=excluded.brand, variant=excluded.variant,
                pack_count=excluded.pack_count, unit_size_ml=excluded.unit_size_ml,
                package_type=excluded.package_type, search_term=excluded.search_term
            """,
            (pack.catalog_id, pack.name, pack.brand, pack.variant, pack.pack_count,
             pack.unit_size_ml, pack.package_type, pack.search_term),
        )
        connection.execute(
            """
            INSERT INTO catalog_mappings
                (catalog_id, retailer, expected_product_name,
                 source_product_reference, source_item_id, status)
            VALUES (?, 'lidl', ?, ?, ?, ?)
            ON CONFLICT(catalog_id, retailer) DO UPDATE SET
                expected_product_name=excluded.expected_product_name,
                source_product_reference=excluded.source_product_reference,
                source_item_id=excluded.source_item_id, status=excluded.status
            """,
            (mapping.catalog_id, mapping.expected_product_name,
             source_product_id or mapping.source_product_id, None, mapping.status),
        )
        if own_run:
            connection.execute(
                """
                INSERT INTO collection_runs
                    (run_id, started_at, finished_at, status, observed_count, failed_count, summary)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (run_id, started_at, timestamp(),
                 "completed" if status != "source_error" else "failed",
                 summary["observed_count"], summary["failed_count"], json.dumps(summary)),
            )
        connection.execute(
            """
            INSERT INTO collection_results
                (run_id, catalog_id, retailer, status, error,
                 source_product_reference, source_item_id, source_scope,
                 complete, recorded_at)
            VALUES (?, ?, 'lidl', ?, ?, ?, NULL, NULL, ?, ?)
            """,
            (run_id, pack.catalog_id, status, error,
             source_product_id or None, complete, timestamp()),
        )
        if status == "observed":
            assert item is not None and displayed_price is not None
            connection.execute(
                """
                INSERT INTO price_observations (
                    run_id, catalog_id, retailer, source_product_reference,
                    source_item_id, source_product_name, displayed_price, clubcard_price,
                    drs_deposit, source_scope, currency, pack_count, unit_size_ml,
                    package_type, component_unit_price, price_per_litre, observed_at
                ) VALUES (?, ?, 'lidl', ?, ?, ?, ?, NULL, ?, NULL, 'EUR', ?, ?, ?, ?, ?, ?)
                """,
                (run_id, pack.catalog_id, source_product_id, source_product_id,
                 item.get("name", ""), _decimal_text(displayed_price),
                 _decimal_text(drs_deposit) if drs_deposit is not None else None,
                 pack.pack_count, pack.unit_size_ml, pack.package_type,
                 component_unit_price, price_per_litre, timestamp()),
            )
        connection.commit()
    return summary | ({"error": error} if error else {})


ALDI_SEARCH_ENDPOINT = "https://asl.api.aldi.ie/commerce/v3/product-search"
ALDI_PRODUCT_ENDPOINT = "https://asl.api.aldi.ie/commerce/v2/products"
ALDI_SEARCH_LIMIT = 30  # the API rejects page sizes outside {12,16,24,30,32,48,60}


def _aldi_record(item: Mapping[str, Any]) -> dict[str, Any]:
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
        record["price"] = "€{}".format(_decimal_text(Decimal(str(price["amount"])) / 100))
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
            or "€{}".format(_decimal_text(Decimal(str(price["bottleDeposit"])) / 100))
        )
    return record


class AldiClient:
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
        self.opener = opener or urllib.request.build_opener()
        self.min_request_interval = min_request_interval
        self._last_request_at: float | None = None

    def __call__(self, search_term: str) -> dict[str, Any]:
        """Search Aldi Ireland products."""
        if not search_term.strip():
            raise ValueError("Aldi search term must not be empty")
        payload = self._request_json(
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
            "items": [_aldi_record(item) for item in items if isinstance(item, Mapping)],
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
        payload = self._request_json(
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
            _aldi_record(item)
            for item in (items if isinstance(items, list) else [])
            if isinstance(item, Mapping)
        ]
        matching = [r for r in records if r["productId"] == identifier]
        return {"items": matching}

    def _throttle(self) -> None:
        if self._last_request_at is not None:
            delay = self.min_request_interval - (time.monotonic() - self._last_request_at)
            if delay > 0:
                time.sleep(delay)

    def _request_json(self, url: str) -> Any:
        self._throttle()
        request = urllib.request.Request(
            url,
            headers={"Accept": "application/json", "User-Agent": "drinks-tracker/0.1"},
        )
        try:
            with self.opener.open(request, timeout=30) as response:
                if getattr(response, "status", 200) >= 400:
                    raise RuntimeError(f"Aldi HTTP {response.status}")
                body = response.read()
        except Exception as exc:
            if isinstance(exc, RuntimeError):
                raise
            raise RuntimeError(f"Aldi request failed: {exc}") from exc
        finally:
            self._last_request_at = time.monotonic()
        try:
            return json.loads(body.decode("utf-8"))
        except (UnicodeDecodeError, ValueError) as exc:
            raise RuntimeError(f"Aldi response was not valid JSON: {exc}") from exc


def _find_aldi_listing(
    payload: Mapping[str, Any], mapping: AldiMapping
) -> dict[str, Any]:
    items = payload.get("items")
    if not isinstance(items, list):
        raise ValueError("Aldi response has no items list")
    expected_tokens = _normalise_name(mapping.expected_product_name)
    for item in items:
        if not isinstance(item, dict):
            continue
        product_id = str(item.get("productId") or "")
        if mapping.source_product_id and product_id != str(mapping.source_product_id):
            continue
        if not mapping.source_product_id and not expected_tokens.issubset(
            _normalise_name(str(item.get("name") or ""))
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
    return _decimal_price(text)


def collect_aldi_one(
    pack: BenchmarkPack,
    mapping: AldiMapping,
    fetcher: Callable[[str], Mapping[str, Any]],
    database: str | Path,
    *,
    _run_id: str | None = None,
    _started_at: str | None = None,
) -> dict[str, Any]:
    """Collect one mapped pack from Aldi Ireland's grocery API."""
    if pack.catalog_id != mapping.catalog_id:
        raise ValueError("catalog pack and Aldi mapping must have the same catalog_id")
    if pack.pack_count < 1 or pack.unit_size_ml < 1:
        raise ValueError("pack composition must contain positive count and size")

    started_at = _started_at or timestamp()
    started = time.monotonic()
    run_id = _run_id or uuid.uuid4().hex
    own_run = _run_id is None
    status = "observed"
    error: str | None = None
    item: dict[str, Any] | None = None
    payload: Mapping[str, Any] | None = None
    complete = "unknown"
    if mapping.status != "approved":
        status = "unmapped"
        error = "catalog mapping is not approved"
    else:
        try:
            direct_fetcher = getattr(fetcher, "fetch_product", None)
            payload = (
                direct_fetcher(str(mapping.source_product_id))
                if mapping.source_product_id and callable(direct_fetcher)
                else fetcher(pack.search_term)
            )
            complete = _page_completeness(payload)
            item = _find_aldi_listing(payload, mapping)
            # Aldi keeps the brand in a structured field rather than the
            # product name, so validate against the combined evidence.
            evidence_name = " ".join(
                str(part) for part in (item.get("brand"), item.get("name")) if part
            )
            reason = _validate_listing(evidence_name, pack)
            if reason is not None:
                status = "source_error"
                error = f"stale source identifier: {reason}"
        except LookupError as exc:
            status = _absence_status(payload)
            error = str(exc)
        except Exception as exc:
            status = "source_error"
            error = str(exc)

    displayed_price: Decimal | None = None
    drs_deposit: Decimal | None = None
    component_unit_price: str | None = None
    price_per_litre: str | None = None
    if status == "observed":
        assert item is not None
        try:
            displayed_price = _decimal_price(item.get("price"))
            drs_deposit = _aldi_drs_deposit(item)
            component_unit_price = _decimal_text(displayed_price / pack.pack_count)
            litres = Decimal(pack.pack_count * pack.unit_size_ml) / Decimal(1000)
            price_per_litre = _decimal_text(displayed_price / litres, "0.0001")
        except Exception as exc:
            status = "source_error"
            error = str(exc)

    source_product_id = (
        str(item.get("productId") or "") if item else str(mapping.source_product_id or "")
    )
    summary = {
        "run_id": run_id,
        "retailer": "aldi",
        "catalog_id": pack.catalog_id,
        "status": status,
        "complete": complete,
        "observed_count": int(status == "observed"),
        "failed_count": int(status == "source_error"),
        "duration_ms": round((time.monotonic() - started) * 1000, 1),
    }

    database_path = Path(database)
    database_path.parent.mkdir(parents=True, exist_ok=True)
    with closing(sqlite3.connect(database_path)) as connection:
        ensure_schema(connection)
        connection.execute(
            """
            INSERT INTO catalog_packs VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(catalog_id) DO UPDATE SET
                name=excluded.name, brand=excluded.brand, variant=excluded.variant,
                pack_count=excluded.pack_count, unit_size_ml=excluded.unit_size_ml,
                package_type=excluded.package_type, search_term=excluded.search_term
            """,
            (pack.catalog_id, pack.name, pack.brand, pack.variant, pack.pack_count,
             pack.unit_size_ml, pack.package_type, pack.search_term),
        )
        connection.execute(
            """
            INSERT INTO catalog_mappings
                (catalog_id, retailer, expected_product_name,
                 source_product_reference, source_item_id, status)
            VALUES (?, 'aldi', ?, ?, ?, ?)
            ON CONFLICT(catalog_id, retailer) DO UPDATE SET
                expected_product_name=excluded.expected_product_name,
                source_product_reference=excluded.source_product_reference,
                source_item_id=excluded.source_item_id, status=excluded.status
            """,
            (mapping.catalog_id, mapping.expected_product_name,
             source_product_id or mapping.source_product_id, None, mapping.status),
        )
        if own_run:
            connection.execute(
                """
                INSERT INTO collection_runs
                    (run_id, started_at, finished_at, status, observed_count, failed_count, summary)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (run_id, started_at, timestamp(),
                 "completed" if status != "source_error" else "failed",
                 summary["observed_count"], summary["failed_count"], json.dumps(summary)),
            )
        connection.execute(
            """
            INSERT INTO collection_results
                (run_id, catalog_id, retailer, status, error,
                 source_product_reference, source_item_id, source_scope,
                 complete, recorded_at)
            VALUES (?, ?, 'aldi', ?, ?, ?, NULL, NULL, ?, ?)
            """,
            (run_id, pack.catalog_id, status, error,
             source_product_id or None, complete, timestamp()),
        )
        if status == "observed":
            assert item is not None and displayed_price is not None
            connection.execute(
                """
                INSERT INTO price_observations (
                    run_id, catalog_id, retailer, source_product_reference,
                    source_item_id, source_product_name, displayed_price, clubcard_price,
                    drs_deposit, source_scope, currency, pack_count, unit_size_ml,
                    package_type, component_unit_price, price_per_litre, observed_at
                ) VALUES (?, ?, 'aldi', ?, ?, ?, ?, NULL, ?, NULL, 'EUR', ?, ?, ?, ?, ?, ?)
                """,
                (run_id, pack.catalog_id, source_product_id, source_product_id,
                 item.get("name", ""), _decimal_text(displayed_price),
                 _decimal_text(drs_deposit) if drs_deposit is not None else None,
                 pack.pack_count, pack.unit_size_ml, pack.package_type,
                 component_unit_price, price_per_litre, timestamp()),
            )
        connection.commit()
    return summary | ({"error": error} if error else {})


def upsert_catalog_pack(connection: sqlite3.Connection, pack: BenchmarkPack) -> None:
    connection.execute(
        """
        INSERT INTO catalog_packs VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(catalog_id) DO UPDATE SET
            name=excluded.name, brand=excluded.brand, variant=excluded.variant,
            pack_count=excluded.pack_count, unit_size_ml=excluded.unit_size_ml,
            package_type=excluded.package_type, search_term=excluded.search_term
        """,
        (
            pack.catalog_id, pack.name, pack.brand, pack.variant, pack.pack_count,
            pack.unit_size_ml, pack.package_type, pack.search_term,
        ),
    )


def _record_collection_result(
    database: str | Path,
    run_id: str,
    pack: BenchmarkPack,
    retailer: str,
    status: str,
    error: str | None,
    source_scope: str | None,
) -> None:
    with closing(sqlite3.connect(database)) as connection:
        ensure_schema(connection)
        upsert_catalog_pack(connection, pack)
        connection.execute(
            """
            INSERT INTO collection_results
                (run_id, catalog_id, retailer, status, error,
                 source_product_reference, source_item_id, source_scope,
                 complete, recorded_at)
            VALUES (?, ?, ?, ?, ?, NULL, NULL, ?, 'unknown', ?)
            """,
            (run_id, pack.catalog_id, retailer, status, error, source_scope, timestamp()),
        )
        connection.commit()


def _log_decision(
    run_id: str,
    retailer: str,
    pack: BenchmarkPack,
    result: Mapping[str, Any],
    *,
    mapping_configured: bool,
) -> None:
    """Emit one structured line per retailer-pack decision.

    This is the per-product audit trail for "why is this product missing?":
    every cell is logged exactly once with the stage that accepted or
    rejected it (unmapped → no approved mapping; not_found / source_error /
    inconclusive → rejected at collection with the reason; observed → became
    an observation).
    """
    status = result["status"]
    level = {
        "observed": logging.INFO,
        "unmapped": logging.INFO,
        "not_found": logging.WARNING,
        "source_error": logging.ERROR,
        "inconclusive": logging.WARNING,
    }.get(status, logging.WARNING)
    fields = [
        f"run={run_id}",
        f"retailer={retailer}",
        f"pack={pack.catalog_id}",
        f"decision={status}",
        f"mapping={'configured' if mapping_configured else 'missing'}",
    ]
    if result.get("error"):
        fields.append(f"reason={result['error']}")
    if result.get("source_product_reference"):
        fields.append(f"ref={result['source_product_reference']}")
    logger.log(level, " ".join(fields))


def _mapping_rows(value: Any) -> list[Any]:
    if value is None:
        return []
    if isinstance(value, Mapping):
        return list(value.values())
    return list(value)


def collect_run(
    catalog: list[BenchmarkPack],
    mappings: Mapping[str, Any],
    adapters: Mapping[str, Callable[[str], Mapping[str, Any]]],
    database: str | Path,
    *,
    retailer: str | None = None,
    catalog_id: str | None = None,
    store_ids: Mapping[str, str] | None = None,
    max_retries: int = 2,
    retry_backoff: float = 0.5,
) -> dict[str, Any]:
    """Run the active catalog matrix with isolated retailer-pack results."""
    if max_retries < 0 or retry_backoff < 0:
        raise ValueError("retry settings must not be negative")

    selected_catalog = [
        pack for pack in catalog
        if catalog_id is None or pack.catalog_id == catalog_id
    ]
    selected_retailers = [
        name for name in adapters
        if retailer is None or name == retailer
    ]
    started_at = timestamp()
    started = time.monotonic()
    run_id = uuid.uuid4().hex
    database_path = Path(database)
    database_path.parent.mkdir(parents=True, exist_ok=True)
    with closing(sqlite3.connect(database_path)) as connection:
        ensure_schema(connection)
        connection.execute(
            """
            INSERT INTO collection_runs
                (run_id, started_at, finished_at, status, observed_count, failed_count, summary)
            VALUES (?, ?, ?, 'running', 0, 0, ?)
            """,
            (run_id, started_at, started_at, json.dumps({"status": "running"})),
        )
        connection.commit()

    summary: dict[str, Any] = {
        "run_id": run_id,
        "started_at": started_at,
        "retailer": retailer,
        "catalog_id": catalog_id,
        "attempted_count": 0,
        "mapped_count": 0,
        "observed_count": 0,
        "failed_count": 0,
        "not_found_count": 0,
        "inconclusive_count": 0,
        "unmapped_count": 0,
        "affected_retailers": set(),
        "affected_catalog_ids": set(),
        "unmapped_retailers": set(),
        "unmapped_catalog_ids": set(),
    }
    store_ids = store_ids or {}

    for retailer_name in selected_retailers:
        rows = {
            row.catalog_id: row
            for row in _mapping_rows(mappings.get(retailer_name))
            if getattr(row, "catalog_id", None)
        }
        adapter = adapters[retailer_name]
        for pack in selected_catalog:
            summary["attempted_count"] += 1
            mapping = rows.get(pack.catalog_id)
            scope = store_ids.get(retailer_name) or getattr(adapter, "store_id", None)
            result: dict[str, Any]
            if mapping is None:
                result = {
                    "status": "unmapped",
                    "error": "no catalog mapping configured",
                    "observed_count": 0,
                    "failed_count": 0,
                }
                _record_collection_result(
                    database_path, run_id, pack, retailer_name,
                    result["status"], result["error"], scope,
                )
            else:
                summary["mapped_count"] += 1
                fetcher = _retrying_fetcher(
                    adapter,
                    database=database_path,
                    run_id=run_id,
                    retailer=retailer_name,
                    catalog_id=pack.catalog_id,
                    max_retries=max_retries,
                    retry_backoff=retry_backoff,
                    direct_fetcher=getattr(adapter, "fetch_product", None),
                )
                try:
                    if retailer_name == "dunnes":
                        result = collect_one(
                            pack, mapping, fetcher, database_path,
                            _run_id=run_id, _started_at=started_at,
                        )
                    elif retailer_name == "supervalu":
                        result = collect_supervalu_one(
                            pack, mapping, fetcher, database_path,
                            store_id=scope, _run_id=run_id, _started_at=started_at,
                        )
                    elif retailer_name == "tesco":
                        result = collect_tesco_one(
                            pack, mapping, fetcher, database_path,
                            _run_id=run_id, _started_at=started_at,
                        )
                    elif retailer_name == "lidl":
                        result = collect_lidl_one(
                            pack, mapping, fetcher, database_path,
                            _run_id=run_id, _started_at=started_at,
                        )
                    elif retailer_name == "aldi":
                        result = collect_aldi_one(
                            pack, mapping, fetcher, database_path,
                            _run_id=run_id, _started_at=started_at,
                        )
                    else:
                        raise ValueError(f"unsupported retailer adapter: {retailer_name}")
                except Exception as exc:
                    result = {
                        "status": "source_error",
                        "error": str(exc),
                        "observed_count": 0,
                        "failed_count": 1,
                    }
                    _record_collection_result(
                        database_path, run_id, pack, retailer_name,
                        result["status"], result["error"], scope,
                    )

            status = result["status"]
            _log_decision(
                run_id,
                retailer_name,
                pack,
                result,
                mapping_configured=mapping is not None,
            )
            summary["observed_count"] += result.get("observed_count", 0)
            if status == "source_error":
                summary["failed_count"] += 1
            elif status == "not_found":
                summary["not_found_count"] += 1
            elif status == "inconclusive":
                summary["inconclusive_count"] += 1
            elif status == "unmapped":
                summary["unmapped_count"] += 1
            if status in {"source_error", "not_found", "inconclusive"}:
                summary["affected_retailers"].add(retailer_name)
                summary["affected_catalog_ids"].add(pack.catalog_id)
                _record_diagnostic(
                    database_path, run_id, retailer_name, pack.catalog_id, "result",
                    level="error" if status == "source_error" else "warning",
                    message=result.get("error") or status,
                )
            elif status == "unmapped":
                summary["unmapped_retailers"].add(retailer_name)
                summary["unmapped_catalog_ids"].add(pack.catalog_id)

    summary["affected_retailers"] = sorted(summary["affected_retailers"])
    summary["affected_catalog_ids"] = sorted(summary["affected_catalog_ids"])
    summary["unmapped_retailers"] = sorted(summary["unmapped_retailers"])
    summary["unmapped_catalog_ids"] = sorted(summary["unmapped_catalog_ids"])
    if summary["unmapped_count"]:
        _record_diagnostic(
            database_path,
            run_id,
            "collection",
            None,
            "unmapped_summary",
            level="info",
            message=(
                f"{summary['unmapped_count']} retailer-pack cells have no configured "
                "Catalog Mapping"
            ),
            request_metadata={
                "unmapped_count": summary["unmapped_count"],
                "retailers": summary["unmapped_retailers"],
                "catalog_id_count": len(summary["unmapped_catalog_ids"]),
            },
        )
    finished_at = timestamp()
    summary["finished_at"] = finished_at
    summary["duration_ms"] = round((time.monotonic() - started) * 1000, 1)
    summary["status"] = (
        "failed" if summary["failed_count"] and not summary["observed_count"]
        else "partial" if summary["failed_count"]
        else "completed"
    )
    with closing(sqlite3.connect(database_path)) as connection:
        ensure_schema(connection)
        connection.execute(
            """
            UPDATE collection_runs
            SET finished_at = ?, status = ?, observed_count = ?, failed_count = ?, summary = ?
            WHERE run_id = ?
            """,
            (
                finished_at, summary["status"], summary["observed_count"],
                summary["failed_count"], json.dumps(summary), run_id,
            ),
        )
        connection.commit()
    return summary


def as_datetime(value: datetime | str | None) -> datetime:
    if value is None:
        return datetime.now(timezone.utc)
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=timezone.utc)
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def _iso(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def purge_retention(
    database: str | Path,
    *,
    now: datetime | str | None = None,
    raw_days: int = 90,
    dormant_days: int = 180,
    purge_days: int = 365,
) -> dict[str, int]:
    """Apply operational retention and mark or remove stale mappings."""
    if not (0 <= raw_days <= dormant_days <= purge_days):
        raise ValueError("retention periods must be ordered and non-negative")
    current = as_datetime(now)
    raw_cutoff = _iso(current - timedelta(days=raw_days))
    dormant_cutoff = _iso(current - timedelta(days=dormant_days))
    purge_cutoff = _iso(current - timedelta(days=purge_days))
    counts = {
        "deleted_diagnostics": 0,
        "deleted_candidates": 0,
        "dormant_mappings": 0,
        "purged_observations": 0,
        "purged_mappings": 0,
    }
    with closing(sqlite3.connect(database)) as connection:
        ensure_schema(connection)
        counts["deleted_diagnostics"] = connection.execute(
            "DELETE FROM collection_diagnostics WHERE created_at < ?", (raw_cutoff,)
        ).rowcount
        counts["deleted_candidates"] = connection.execute(
            "DELETE FROM catalog_candidates WHERE first_seen_at < ?", (raw_cutoff,)
        ).rowcount
        stale = connection.execute(
            """
            SELECT cm.catalog_id, cm.retailer, MAX(po.observed_at) AS last_seen
            FROM catalog_mappings AS cm
            JOIN price_observations AS po
              ON po.catalog_id = cm.catalog_id AND po.retailer = cm.retailer
            GROUP BY cm.catalog_id, cm.retailer
            """
        ).fetchall()
        for catalog_id_value, retailer_value, last_seen_at in stale:
            if last_seen_at <= purge_cutoff:
                counts["purged_observations"] += connection.execute(
                    "DELETE FROM price_observations WHERE catalog_id = ? AND retailer = ?",
                    (catalog_id_value, retailer_value),
                ).rowcount
                counts["purged_mappings"] += connection.execute(
                    "DELETE FROM catalog_mappings WHERE catalog_id = ? AND retailer = ?",
                    (catalog_id_value, retailer_value),
                ).rowcount
            elif last_seen_at <= dormant_cutoff:
                counts["dormant_mappings"] += connection.execute(
                    """
                    UPDATE catalog_mappings SET status = 'dormant'
                    WHERE catalog_id = ? AND retailer = ? AND status <> 'dormant'
                    """,
                    (catalog_id_value, retailer_value),
                ).rowcount
        connection.commit()
    return counts


def load_catalog(catalog_path: Path) -> list[BenchmarkPack]:
    rows = json.loads(catalog_path.read_text())
    if not isinstance(rows, list):
        raise ValueError("catalog file must contain a list")
    packs = []
    for row in rows:
        if not isinstance(row, dict):
            raise ValueError("catalog entries must be objects")
        row = dict(row)
        row["aliases"] = tuple(row.get("aliases", ()))
        packs.append(BenchmarkPack(**row))
    return packs


def _load_mappings(mapping_path: Path) -> dict[str, list[Any]]:
    raw = json.loads(mapping_path.read_text())
    if isinstance(raw, list):
        # Preserve the original Dunnes-only mapping file format.
        raw = {"dunnes": raw}
    if not isinstance(raw, dict):
        raise ValueError("mapping file must contain a retailer mapping object")

    mapping_types = {
        "dunnes": DunnesMapping,
        "supervalu": SuperValuMapping,
        "tesco": TescoMapping,
        "lidl": LidlMapping,
        "aldi": AldiMapping,
    }
    mappings: dict[str, list[Any]] = {}
    for retailer, rows in raw.items():
        mapping_type = mapping_types.get(retailer)
        if mapping_type is None:
            raise ValueError(f"unsupported retailer in mapping file: {retailer}")
        if not isinstance(rows, list):
            raise ValueError(f"mappings for {retailer} must be a list")
        allowed = {field.name for field in fields(mapping_type)}
        mappings[retailer] = [mapping_type(**{key: value for key, value in row.items() if key in allowed}) for row in rows]
    return mappings


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Collect the local beverage price feed")
    parser.add_argument("--catalog", type=Path, default=Path("data/catalog.json"))
    parser.add_argument("--mapping", type=Path, default=Path("data/mappings.json"))
    parser.add_argument("--catalog-id", help="stable catalog_id to collect")
    parser.add_argument("--retailer", choices=("dunnes", "supervalu", "tesco", "lidl", "aldi"))
    parser.add_argument(
        "--supervalu-store-id",
        default=os.environ.get("SUPERVALU_STORE_ID"),
        help="configured SuperValu store identifier (or SUPERVALU_STORE_ID)",
    )
    parser.add_argument(
        "--database",
        type=Path,
        default=Path(os.environ.get("DRINKS_DATABASE", "data/feed.sqlite")),
    )
    args = parser.parse_args(argv)

    # Per-cell decision logs go to stderr; the operator-facing run summary is
    # printed at the end of main(). Override with DRINKS_LOG_LEVEL if needed.
    logging.basicConfig(
        level=os.environ.get("DRINKS_LOG_LEVEL", "INFO").upper(),
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )

    catalog = load_catalog(args.catalog)
    if args.catalog_id and not any(pack.catalog_id == args.catalog_id for pack in catalog):
        raise ValueError(f"catalog pack not found: {args.catalog_id}")
    mappings = _load_mappings(args.mapping)
    configured_retailers = [name for name, rows in mappings.items() if rows]
    if args.retailer:
        configured_retailers = [args.retailer]
        mappings.setdefault(args.retailer, [])
    if not configured_retailers:
        raise ValueError("no configured retailer mappings found")
    if "supervalu" in configured_retailers and not args.supervalu_store_id:
        parser.error("--supervalu-store-id or SUPERVALU_STORE_ID is required for SuperValu")

    adapters: dict[str, Callable[[str], Mapping[str, Any]]] = {}
    for retailer in configured_retailers:
        try:
            if retailer == "dunnes":
                adapters[retailer] = DunnesClient()
            elif retailer == "supervalu":
                adapters[retailer] = SuperValuClient(args.supervalu_store_id)
            elif retailer == "tesco":
                adapters[retailer] = TescoClient()
            elif retailer == "lidl":
                # Collection runs use the focused Lidl IE client from
                # lidl.py (ticket 09); same fetcher contract as the inline
                # stub above, plus the detail endpoint and title pack parsing.
                from .lidl import LidlClient as WorkingLidlClient

                adapters[retailer] = WorkingLidlClient()
            elif retailer == "aldi":
                # Collection runs use the focused Aldi Glue client from
                # aldi.py (ticket 10); same fetcher contract as the inline
                # stub above, plus servicePoint and sellingSize pack parsing.
                from .aldi import AldiClient as WorkingAldiClient

                adapters[retailer] = WorkingAldiClient()
        except ValueError as exc:
            # A single unconfigured retailer (e.g. missing API key) must not
            # block collection across every other configured retailer.
            print(f"skipping {retailer}: {exc}", file=sys.stderr)
            configured_retailers.remove(retailer)
    if not adapters:
        raise ValueError("no collectable retailers; configure the missing credentials")

    summary = collect_run(
        catalog,
        mappings,
        adapters,
        args.database,
        retailer=args.retailer,
        catalog_id=args.catalog_id,
        store_ids={"supervalu": args.supervalu_store_id},
    )
    affected_retailers = ",".join(summary["affected_retailers"]) or "-"
    print(
        f"collection {summary['status']}: run={summary['run_id']} "
        f"finished={summary['finished_at']} "
        f"attempted={summary['attempted_count']} "
        f"mapped={summary['mapped_count']} "
        f"observed={summary['observed_count']} "
        f"not_found={summary['not_found_count']} "
        f"inconclusive={summary['inconclusive_count']} "
        f"unmapped={summary['unmapped_count']} "
        f"failed={summary['failed_count']} "
        f"duration_ms={summary['duration_ms']} "
        f"affected_retailers={affected_retailers} "
        f"affected_catalogs={len(summary['affected_catalog_ids'])}"
    )
    return 0 if summary["status"] == "completed" else 1


if __name__ == "__main__":
    raise SystemExit(main())
