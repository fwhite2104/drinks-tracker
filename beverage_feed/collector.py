"""The first local collection seam and Dunnes VTEX adapter."""

from __future__ import annotations

import argparse
import http.cookiejar
from contextlib import closing
import json
import os
import re
import sqlite3
import time
import urllib.parse
import urllib.request
import uuid
from dataclasses import dataclass, fields
from datetime import datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
from pathlib import Path
from typing import Any, Callable, Mapping


DUNNES_ENDPOINT = "https://www.dunnesstores.com/_v/segment/graphql/v1"


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


class DunnesClient:
    """Fetch the small VTEX GraphQL response needed by the collector."""

    def __init__(self, endpoint: str = DUNNES_ENDPOINT):
        self.endpoint = endpoint

    def __call__(self, search_term: str) -> dict[str, Any]:
        query = f'''query {{
          productSearch(fullText: {json.dumps(search_term)}, from: 0, to: 49)
            @context(provider: "vtex.search-graphql@0.72.0") {{
            products {{
              productName
              productReference
              items {{
                itemId
                sellers {{
                  commertialOffer {{ Price ListPrice }}
                }}
              }}
            }}
          }}
        }}'''
        request = urllib.request.Request(
            self.endpoint,
            data=json.dumps({"query": query}).encode(),
            headers={
                "Content-Type": "application/json",
                "Accept": "application/json",
                "User-Agent": "drinks-tracker/0.1",
            },
            method="POST",
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

        if payload.get("errors"):
            message = payload["errors"][0].get("message", "GraphQL error")
            raise RuntimeError(f"Dunnes GraphQL error: {message}")
        return payload


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
"""


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


def _normalise_name(value: str) -> set[str]:
    return set(re.findall(r"[a-z0-9]+", value.lower().replace("-", " ")))


def _find_listing(
    payload: Mapping[str, Any], mapping: DunnesMapping
) -> tuple[dict[str, Any], dict[str, Any], dict[str, Any]]:
    products = payload.get("data", {}).get("productSearch", {}).get("products")
    if not isinstance(products, list):
        raise ValueError("Dunnes response has no productSearch.products list")

    expected_tokens = _normalise_name(mapping.expected_product_name)
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
            sellers = item.get("sellers") or []
            offer = sellers[0].get("commertialOffer") if sellers else None
            if isinstance(offer, dict) and offer.get("Price") is not None:
                return product, item, offer
    raise LookupError("mapped Dunnes product was not found")


def ensure_schema(connection: sqlite3.Connection) -> None:
    connection.executescript(SCHEMA)
    connection.execute("PRAGMA foreign_keys = ON")
    # Keep a database created by the first Dunnes-only milestone usable.
    for table, column, definition in (
        ("collection_results", "source_scope", "TEXT"),
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

    The latest result wins even when it is ``not_found`` or ``source_error``;
    this prevents an older price from being presented as current. Results for
    other retailer-pack pairs are independent.
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
        )
        SELECT {_OBSERVATION_COLUMNS}
        FROM latest_results AS lr
        JOIN price_observations AS po
          ON po.run_id = lr.run_id
         AND po.catalog_id = lr.catalog_id
         AND po.retailer = lr.retailer
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

    if mapping.status != "approved":
        status = "unmapped"
        error = "catalog mapping is not approved"
    else:
        try:
            payload = fetcher(pack.search_term)
            product, item, offer = _find_listing(payload, mapping)
        except LookupError as exc:
            status = "not_found"
            error = str(exc)
        except Exception as exc:
            status = "source_error"
            error = str(exc)

    observed_at = timestamp()
    duration_ms = round((time.monotonic() - started) * 1000, 1)
    observed = status == "observed"
    displayed_price: Decimal | None = None
    component_unit_price: str | None = None
    price_per_litre: str | None = None

    if observed:
        assert product is not None and item is not None and offer is not None
        displayed_price = _decimal_price(offer["Price"])
        component_unit_price = _decimal_text(displayed_price / pack.pack_count)
        litres = Decimal(pack.pack_count * pack.unit_size_ml) / Decimal(1000)
        price_per_litre = _decimal_text(displayed_price / litres, "0.0001")

    summary = {
        "run_id": run_id,
        "retailer": "dunnes",
        "catalog_id": pack.catalog_id,
        "status": status,
        "observed_count": int(observed),
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
            INSERT INTO catalog_mappings VALUES (?, 'dunnes', ?, ?, ?, ?)
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
            "INSERT INTO collection_results VALUES (?, ?, 'dunnes', ?, ?, ?, ?, NULL, ?)",
            (
                run_id,
                pack.catalog_id,
                status,
                error,
                product.get("productReference") if product else None,
                item.get("itemId") if item else None,
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
        if observed:
            assert product is not None and item is not None and displayed_price is not None
            connection.execute(
                """
                INSERT INTO price_observations (
                    run_id, catalog_id, retailer, source_product_reference,
                    source_item_id, source_product_name, displayed_price, currency,
                    pack_count, unit_size_ml, package_type, component_unit_price,
                    price_per_litre, observed_at
                ) VALUES (?, ?, 'dunnes', ?, ?, ?, ?, 'EUR', ?, ?, ?, ?, ?, ?)
                """,
                (
                    run_id,
                    pack.catalog_id,
                    product.get("productReference", ""),
                    item.get("itemId", ""),
                    product.get("productName", ""),
                    _decimal_text(displayed_price),
                    pack.pack_count,
                    pack.unit_size_ml,
                    pack.package_type,
                    component_unit_price,
                    price_per_litre,
                    observed_at,
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
    _run_id: str | None = None,
    _started_at: str | None = None,
) -> dict[str, Any]:
    """Collect one mapped pack from one configured SuperValu store."""
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

    if mapping.status != "approved":
        status = "unmapped"
        error = "catalog mapping is not approved"
    else:
        try:
            item = _find_supervalu_listing(fetcher(pack.search_term), mapping)
        except LookupError as exc:
            status = "not_found"
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
            INSERT INTO catalog_mappings VALUES (?, 'supervalu', ?, ?, ?, ?)
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
                 source_product_reference, source_item_id, source_scope, recorded_at)
            VALUES (?, ?, 'supervalu', ?, ?, ?, ?, ?, ?)
            """,
            (
                run_id, pack.catalog_id, status, error,
                source_product_id or None,
                source_item_id or None,
                store_id, observed_at,
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
    promotions { description }
  }
}
"""


class TescoClient:
    """Fetch Irish Tesco search results and hydrate them through GraphQL."""

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

    def __call__(self, search_term: str) -> dict[str, Any]:
        if not search_term.strip():
            raise ValueError("Tesco search term must not be empty")
        search_url = self.search_endpoint + "?" + urllib.parse.urlencode(
            {"distchannel": "ghs", "query": search_term, "count": 10, "geo": "ie"}
        )
        search_payload = self._request_json(
            urllib.request.Request(
                search_url,
                headers={"Accept": "application/json", "User-Agent": "drinks-tracker/0.1"},
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
                    "x-apikey": self.api_key,
                    "region": "IE",
                    "language": "en-IE",
                },
                method="POST",
            )
        )
        if not isinstance(detail_payload, list):
            raise RuntimeError("Tesco GraphQL response was not a list")
        products = []
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
    price = _optional_price(item, "clubcardPrice", "clubCardPrice", "loyaltyPrice")
    if price is not None:
        return price
    for promotion in item.get("promotions") or []:
        description = str(promotion.get("description", "")) if isinstance(promotion, dict) else str(promotion)
        if "meal deal" in description.lower():
            continue
        if re.search(r"(?:any\s+)?\d+\s+for\s+€", description, re.IGNORECASE):
            continue
        if re.search(r"club\s*card\s*price", description, re.IGNORECASE):
            match = re.search(r"€\s*([0-9]+(?:[.,][0-9]+)?)", description)
            if match:
                return _decimal_price(match.group(1).replace(",", "."))
    return None


def _tesco_drs_deposit(item: Mapping[str, Any]) -> Decimal | None:
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
            item = _find_tesco_listing(payload, mapping)
        except LookupError as exc:
            status = "not_found"
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
            INSERT INTO catalog_mappings VALUES (?, 'tesco', ?, ?, ?, ?)
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
                 source_product_reference, source_item_id, source_scope, recorded_at)
            VALUES (?, ?, 'tesco', ?, ?, ?, ?, NULL, ?)
            """,
            (run_id, pack.catalog_id, status, error,
             source_product_reference or None, source_item_id or None, timestamp()),
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
                 source_product_reference, source_item_id, source_scope, recorded_at)
            VALUES (?, ?, ?, ?, ?, NULL, NULL, ?, ?)
            """,
            (run_id, pack.catalog_id, retailer, status, error, source_scope, timestamp()),
        )
        connection.commit()


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
            summary["observed_count"] += result.get("observed_count", 0)
            if status == "source_error":
                summary["failed_count"] += 1
            elif status == "not_found":
                summary["not_found_count"] += 1
            elif status == "unmapped":
                summary["unmapped_count"] += 1
            if status in {"source_error", "not_found"}:
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
    parser.add_argument("--retailer", choices=("dunnes", "supervalu", "tesco"))
    parser.add_argument(
        "--supervalu-store-id",
        default=os.environ.get("SUPERVALU_STORE_ID"),
        help="configured SuperValu store identifier (or SUPERVALU_STORE_ID)",
    )
    parser.add_argument("--database", type=Path, default=Path("feed.sqlite"))
    args = parser.parse_args(argv)

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
        if retailer == "dunnes":
            adapters[retailer] = DunnesClient()
        elif retailer == "supervalu":
            adapters[retailer] = SuperValuClient(args.supervalu_store_id)
        elif retailer == "tesco":
            adapters[retailer] = TescoClient()

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
        f"unmapped={summary['unmapped_count']} "
        f"failed={summary['failed_count']} "
        f"duration_ms={summary['duration_ms']} "
        f"affected_retailers={affected_retailers} "
        f"affected_catalogs={len(summary['affected_catalog_ids'])}"
    )
    return 0 if summary["status"] == "completed" else 1


if __name__ == "__main__":
    raise SystemExit(main())
