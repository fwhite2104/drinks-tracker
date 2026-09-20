"""The first local collection seam and Dunnes VTEX adapter."""

from __future__ import annotations

import argparse
import fcntl
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
from dataclasses import dataclass, field, fields, replace
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path
from typing import Any, Callable, Mapping

from . import source_http
from .feed_reads import current_feed, last_seen, price_history
from .aldi import AldiDiscoveryClient as AldiClient
from .aldi import AldiClient as WorkingAldiClient
from .finders import (
    _aldi_drs_deposit,
    _composition_conflict,
    _find_aldi_listing,
    _find_lidl_listing,
    _find_supervalu_listing,
    _find_tesco_listing,
    _lidl_drs_deposit,
    _listing_matches,
    _name_matches,
    _normalise_name,
    _optional_price,
    _supervalu_drs_deposit,
    _tesco_clubcard_price,
    _tesco_drs_deposit,
)
from .lidl import LidlDiscoveryClient as LidlClient
from .lidl import LidlClient as WorkingLidlClient
from .money import decimal_price as _decimal_price
from .money import decimal_text as _decimal_text

# Backward-compat re-exports: the inline Aldi/Lidl clients were relocated to
# aldi.py/lidl.py (``AldiDiscoveryClient``/``LidlDiscoveryClient``) and are
# re-exported here under their historical names because the discovery adapters
# and the discovery CLI still import them from this module.  Collection runs
# use the focused clients ``aldi.AldiClient``/``lidl.LidlClient`` instead
# (see the registration branches in ``main``).

DUNNES_ENDPOINT = "https://storefrontgateway.dunnesstoresgrocery.com/api/stores"
DUNNES_STORE_ID = os.environ.get("DUNNES_STORE_ID", "258")
DUNNES_PAGE_SIZE = 50  # the ``take`` bound requested from the gateway
# The gateway draws a small, non-deterministic set of HTTP 403s even on the
# impersonated transport — different cells each run, and every one of them
# passes on other runs (CI, 2026-09-20: 2-6 cells of 41 at 1.0s spacing).
# Slower spacing plus one spaced retry (``DunnesClient._search``) absorbs it;
# the daily run can afford ~2 extra minutes for 41 cells.
DUNNES_MIN_REQUEST_INTERVAL = 2.5

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
    # Curated GTIN/EAN when known (ticket 18: the Monster Ultra White /
    # Tesco "Ultra Zero" naming mismatch was only resolvable via GTIN).
    # Absent by default; a listing GTIN then only ever conflicts.
    gtin: str | None = None


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
    separate ``storefrontgateway`` host. Its item shape already carries
    ``Price`` and ``taxDetails`` keys compatible with the downstream
    VTEX-style parsing, so this client translates each result into the
    ``productSearch.products`` envelope the collector expects.

    The gateway used to answer plain urllib requests; since 2026-08-30 it
    answers HTTP 403 to this network, and since ~2026-09-06 to GitHub Actions
    egress as well (every mapped Dunnes cell `source_error`ed for two weeks,
    ticket 19). Requests therefore default to the Chrome-impersonated
    transport — the same escape hatch ``TescoClient`` uses — falling back to
    urllib when curl-cffi is not installed or an explicit ``opener``/``session``
    is injected (the test seams).
    """

    def __init__(
        self,
        endpoint: str = DUNNES_ENDPOINT,
        store_id: str = DUNNES_STORE_ID,
        opener: urllib.request.OpenerDirector | None = None,
        min_request_interval: float = DUNNES_MIN_REQUEST_INTERVAL,
        impersonate: str | None = "chrome",
        session: Any | None = None,
    ):
        self.endpoint = endpoint.rstrip("/")
        self.store_id = store_id
        # opener=None keeps the module-level urllib.request.urlopen as the
        # transport (tests intercept it as the network seam).
        self._transport = source_http.RetailerTransport(
            "Dunnes",
            opener=opener,
            min_request_interval=min_request_interval,
            impersonate=impersonate,
            session=session,
        )

    @property
    def transport(self) -> source_http.RetailerTransport:
        """The shared transport; its impersonation mode is observable."""
        return self._transport

    def _search(self, url: str) -> Any:
        """Fetch one search page, retrying once past a flaky 403.

        The gateway draws HTTP 403 on a small, non-deterministic subset of
        requests even on the impersonated transport: CI run 1 (2026-09-20)
        403'd 2 of 41 cells, run 2 403'd 5, different cells each time, and
        every one of them passes on other runs — edge flakiness, not a block.
        The retry goes through the same throttled transport, so it is spaced
        like any other request, and a persistent block still raises after it
        (403 stays non-retryable at the transport level, as classified).
        """
        try:
            return self._transport.json(url)
        except source_http.SourceHTTPError as exc:
            if exc.status != 403:
                raise
        return self._transport.json(url)

    def __call__(self, search_term: str) -> dict[str, Any]:
        if not search_term.strip():
            raise ValueError("Dunnes search term must not be empty")
        url = "{}/{}/search?{}".format(
            self.endpoint,
            self.store_id,
            urllib.parse.urlencode({"q": search_term, "take": DUNNES_PAGE_SIZE}),
        )
        payload = self._search(url)

        items = payload.get("items") if isinstance(payload, dict) else None
        if not isinstance(items, list):
            raise RuntimeError("Dunnes response has no items list")
        raw_total = payload.get("total") if isinstance(payload, dict) else None

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
        return {
            "data": {"productSearch": {"products": products}},
            # Completeness evidence: the gateway's own total (authoritative when
            # present) plus the bounded page size requested. Fewer items than
            # the page size proves the gateway exhausted its matches; a page at
            # capacity may have been truncated, so absence from it must not be
            # recorded as not_found.
            "items": items,
            "total": raw_total if isinstance(raw_total, int) else len(items),
            "pagination": {"pageSize": DUNNES_PAGE_SIZE},
        }


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
    -- status is "last event wins" (approve/reject/resolve set it globally);
    -- per-cell truth lives in catalog_mappings/discovery_rejections and
    -- discovery_cells, so a candidate approved for one cell can legitimately
    -- read 'rejected' here (rejected as a competitor for a different cell).
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


def _page_completeness(payload: Mapping[str, Any] | None) -> str:
    """Classify a retailer response page as complete, truncated, or unknown.

    Retailer clients normalize completeness evidence into the envelope so
    collection can prove a page covered every match — the same evidence
    discovery records per search. Three evidence shapes are recognised, in
    priority order:

    * ``pagination.total`` (+ optional ``pagination.offset``) — the source
      reports the total match count (Lidl, Aldi, Tesco search).
    * ``pagination.pageSize`` — the client requested a bounded page and
      compares the returned count against it: fewer results than the page
      size proves the source exhausted its matches; a page at capacity may
      have been truncated (Dunnes, SuperValu, Tesco fallback).
    * a top-level ``count`` — the SuperValu gateway reports the match count
      beside ``items``.

    Sources without that evidence stay ``unknown`` rather than claiming a
    complete absence.
    """
    if not isinstance(payload, Mapping):
        return "unknown"
    items = payload.get("items")
    seen = len(items) if isinstance(items, list) else 0
    pagination = payload.get("pagination")
    if isinstance(pagination, Mapping):
        total = pagination.get("total")
        if isinstance(total, int) and not isinstance(total, bool):
            offset = pagination.get("offset", 0)
            if not isinstance(offset, int) or isinstance(offset, bool):
                offset = 0
            return "true" if offset + seen >= total else "false"
        page_size = pagination.get("pageSize")
        if isinstance(page_size, int) and not isinstance(page_size, bool):
            return "true" if seen < page_size else "false"
    count = payload.get("count")
    if isinstance(count, int) and not isinstance(count, bool):
        return "true" if seen >= count else "false"
    return "unknown"


def _absence_status(payload: Mapping[str, Any] | None, *, unknown_status: str = "not_found") -> str:
    """Distinguish a proven ``not_found`` from an ``inconclusive`` page.

    A mapped product absent from a page that provably covered every match is
    genuinely not_found; absence from a truncated page — or from a page with
    no completeness evidence at all, where the search-path caller passes
    ``unknown_status="inconclusive"`` — is inconclusive and must never be
    recorded as a false absence.
    """
    completeness = _page_completeness(payload)
    if completeness == "false":
        return "inconclusive"
    if completeness == "unknown":
        return unknown_status
    return "not_found"


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


# ponytail: Dunnes finder stays here — three-part VTEX product/item/offer
# envelope is the one seam left from the R6 split (see finders.py docstring);
# move it if a fourth retailer ever needs the envelope shape.
def _find_listing(
    payload: Mapping[str, Any], mapping: DunnesMapping
) -> tuple[dict[str, Any], dict[str, Any], dict[str, Any]]:
    products = payload.get("data", {}).get("productSearch", {}).get("products")
    if not isinstance(products, list):
        raise ValueError("Dunnes response has no productSearch.products list")

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
            and not _listing_matches(
                mapping.expected_product_name, product.get("productName", "")
            )
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
    # Composition first: an explicit "12 x 330ml" or "x24" in the listing
    # name must agree with the pack's count/size, whatever the brand says.
    # Validated incident: research/wrong-product-audit-2026-09-20.md (a
    # single-can cell observed at a 12-pack price for five runs).
    conflict = _composition_conflict(name, pack.pack_count, pack.unit_size_ml)
    if conflict is not None:
        return conflict
    name_tokens = _normalise_name(name)
    # Validate only the core brand tokens: the productReference identity was
    # approved by review, so the guard's job is detecting the source reusing
    # the reference for a different product line, not re-litigating variant
    # phrasing ("Sugarfree" vs "Sugar Free", "Original" implied not stated).
    # Retailer titles may use a known pack alias instead (e.g. "Diet Coke"
    # for a Coca-Cola Diet pack), mirroring matching.name_matches. Brandless
    # own-label packs (Ballygowan-style sources that omit the brand) match
    # on the pack name minus generic packaging words instead.
    core = _normalise_name(pack.brand)
    if not core or core.issubset(name_tokens):
        return None
    generic = {"bottle", "bottles", "can", "cans", "pack", "x", "ml", "litre", "litres"}
    name_core = _normalise_name(pack.name) - generic
    if name_core and name_core.issubset(name_tokens):
        return None
    if any(
        _normalise_name(alias) and _normalise_name(alias).issubset(name_tokens)
        for alias in pack.aliases
    ):
        return None
    return f"name mismatch: expected {core} not in {name_tokens}"


def _ensure_observation_cell_index(connection: sqlite3.Connection) -> None:
    """Enforce one observation per run, retailer, pack, and source scope.

    Skipped for legacy or foreign ``price_observations`` layouts that lack the
    collector's columns (e.g. discovery-only databases). For a pre-ticket-07
    table holding duplicate cell observations, the earliest row per cell wins
    so ``uq_price_observations_cell`` can be created without failing.
    """
    required = {"observation_id", "run_id", "retailer", "catalog_id", "source_scope"}
    columns = {row[1] for row in connection.execute(
        "PRAGMA table_info(price_observations)"
    ).fetchall()}
    if not required <= columns:
        return
    if connection.execute(
        "SELECT 1 FROM sqlite_master WHERE type = 'index' "
        "AND name = 'uq_price_observations_cell'"
    ).fetchone() is not None:
        return
    connection.execute(
        """
        DELETE FROM price_observations
        WHERE observation_id > (
            SELECT MIN(p2.observation_id) FROM price_observations AS p2
            WHERE p2.run_id = price_observations.run_id
              AND p2.retailer = price_observations.retailer
              AND p2.catalog_id = price_observations.catalog_id
              AND COALESCE(p2.source_scope, '')
                  = COALESCE(price_observations.source_scope, '')
        )
        """
    )
    connection.execute(
        """
        CREATE UNIQUE INDEX IF NOT EXISTS uq_price_observations_cell
        ON price_observations (run_id, retailer, catalog_id, COALESCE(source_scope, ''))
        """
    )


def _add_columns(
    connection: sqlite3.Connection, table: str, columns: Mapping[str, str]
) -> None:
    """Add any missing columns to ``table`` (idempotent, never destructive)."""
    pragma_cursor = connection.execute(f"PRAGMA table_info({table})")
    try:
        existing = {row[1] for row in pragma_cursor.fetchall()}
    finally:
        pragma_cursor.close()
    for name, definition in columns.items():
        if name not in existing:
            connection.execute(f"ALTER TABLE {table} ADD COLUMN {name} {definition}")


def _migrate_baseline(connection: sqlite3.Connection) -> None:
    """Migration v0 -> v1: core tables, legacy columns, seeds, cell index."""
    connection.executescript(SCHEMA)
    # Keep a database created by the first Dunnes-only milestone usable.
    _add_columns(
        connection,
        "collection_results",
        {"source_scope": "TEXT", "complete": "TEXT"},
    )
    _add_columns(
        connection,
        "price_observations",
        {"clubcard_price": "TEXT", "drs_deposit": "TEXT", "source_scope": "TEXT"},
    )
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
    # Ticket 07: one Price Observation per run/retailer/pack/source scope.
    _ensure_observation_cell_index(connection)


def _table_exists(connection: sqlite3.Connection, name: str) -> bool:
    """Whether a table (or view) of ``name`` exists in this database."""
    return connection.execute(
        "SELECT 1 FROM sqlite_master WHERE name = ?", (name,)
    ).fetchone() is not None


def _columns_of(connection: sqlite3.Connection, table: str) -> set[str]:
    """Column names of ``table`` (empty set when the table is absent)."""
    if not _table_exists(connection, table):
        return set()
    return {
        row[1] for row in connection.execute(f"PRAGMA table_info({table})").fetchall()
    }


def _has_observation_columns(connection: sqlite3.Connection) -> bool:
    """Whether price_observations carries the collector's cell columns.

    Discovery-only databases hold legacy or foreign observation layouts;
    observation-aware SQL must be skipped for those.
    """
    return {"catalog_id", "retailer", "observed_at"} <= _columns_of(
        connection, "price_observations"
    )


# Indexes over discovery evidence tables.  Discovery tables may be absent in
# collector-only databases, so each is created only when its table exists;
# a later ensure_schema() call (e.g. the next collection run) adds them.
_DISCOVERY_EVIDENCE_INDEXES: tuple[tuple[str, str, str], ...] = (
    (
        "discovery_candidate_evidence",
        "ix_discovery_candidate_evidence_cell",
        "discovery_candidate_evidence (retailer, catalog_id, candidate_id, recorded_at)",
    ),
    (
        "discovery_search_history",
        "ix_discovery_search_history_cell",
        "discovery_search_history (retailer, catalog_id, searched_at)",
    ),
    (
        "discovery_cells",
        "ix_discovery_cells_state",
        "discovery_cells (state)",
    ),
)


def _ensure_discovery_evidence_indexes(connection: sqlite3.Connection) -> None:
    """Index discovery evidence tables when they exist in this database."""
    for table, index_name, columns in _DISCOVERY_EVIDENCE_INDEXES:
        if _table_exists(connection, table):
            connection.execute(f"CREATE INDEX IF NOT EXISTS {index_name} ON {columns}")


# Query-supporting indexes for retention and Current Feed at scale:
# latest-result selection, Price Observation history/purge lookups, mapping
# staleness scans, and diagnostics retention.  Each is created only when its
# table exists with the columns the index needs (legacy or foreign layouts,
# e.g. discovery-only databases, are left alone).
_QUERY_INDEXES: tuple[tuple[str, frozenset[str], str], ...] = (
    (
        "collection_results",
        frozenset({"retailer", "catalog_id", "recorded_at"}),
        "CREATE INDEX IF NOT EXISTS ix_collection_results_cell "
        "ON collection_results (retailer, catalog_id, recorded_at DESC)",
    ),
    (
        "collection_results",
        frozenset({"run_id"}),
        "CREATE INDEX IF NOT EXISTS ix_collection_results_run "
        "ON collection_results (run_id)",
    ),
    (
        "price_observations",
        frozenset({"retailer", "catalog_id", "observed_at", "observation_id"}),
        "CREATE INDEX IF NOT EXISTS ix_price_observations_history "
        "ON price_observations (retailer, catalog_id, observed_at DESC, observation_id DESC)",
    ),
    (
        "price_observations",
        frozenset({"run_id"}),
        "CREATE INDEX IF NOT EXISTS ix_price_observations_run "
        "ON price_observations (run_id)",
    ),
    (
        "catalog_mappings",
        frozenset({"status", "last_observed_at"}),
        "CREATE INDEX IF NOT EXISTS ix_catalog_mappings_status "
        "ON catalog_mappings (status, last_observed_at)",
    ),
    (
        "collection_diagnostics",
        frozenset({"run_id"}),
        "CREATE INDEX IF NOT EXISTS ix_collection_diagnostics_run "
        "ON collection_diagnostics (run_id)",
    ),
    (
        "collection_diagnostics",
        frozenset({"created_at"}),
        "CREATE INDEX IF NOT EXISTS ix_collection_diagnostics_created "
        "ON collection_diagnostics (created_at)",
    ),
)


def _migrate_query_indexes_and_mapping_timestamps(
    connection: sqlite3.Connection,
) -> None:
    """Migration v1 -> v2: mapping approval/last-observed timestamps, indexes.

    Existing mappings keep their review decision date as ``approved_at``;
    rows without one anchor from the migration moment.  ``last_observed_at``
    is backfilled from the newest Price Observation per retailer-pack cell.
    """
    _add_columns(
        connection,
        "catalog_mappings",
        {"approved_at": "TEXT", "last_observed_at": "TEXT"},
    )
    connection.execute(
        "UPDATE catalog_mappings SET approved_at = ? WHERE approved_at IS NULL",
        (timestamp(),),
    )
    if _has_observation_columns(connection):
        connection.execute(
            """
            UPDATE catalog_mappings AS cm
            SET last_observed_at = (
                SELECT MAX(po.observed_at) FROM price_observations AS po
                WHERE po.catalog_id = cm.catalog_id AND po.retailer = cm.retailer
            )
            WHERE cm.last_observed_at IS NULL
              AND EXISTS (
                  SELECT 1 FROM price_observations AS po
                  WHERE po.catalog_id = cm.catalog_id AND po.retailer = cm.retailer
              )
            """
        )
    for table, required, statement in _QUERY_INDEXES:
        if required <= _columns_of(connection, table):
            connection.execute(statement)
    # The unique cell index is enforced (not just performance): a v1 database
    # whose index was dropped gets it recreated here too.
    _ensure_observation_cell_index(connection)
    _ensure_discovery_evidence_indexes(connection)


_MIGRATIONS: tuple[Callable[[sqlite3.Connection], None], ...] = (
    _migrate_baseline,
    _migrate_query_indexes_and_mapping_timestamps,
)

#: Current schema version stamped into ``PRAGMA user_version``.
SCHEMA_VERSION = len(_MIGRATIONS)


def ensure_schema(connection: sqlite3.Connection) -> None:
    """Create or migrate the collector schema forward, versioned.

    Migrations are selected by ``PRAGMA user_version`` and are idempotent, so
    pre-versioning databases (``user_version`` 0 with tables already present)
    upgrade in place without data loss.
    """
    version = connection.execute("PRAGMA user_version").fetchone()[0]
    if version < SCHEMA_VERSION:
        for migration in _MIGRATIONS[version:]:
            migration(connection)
        connection.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")
        connection.commit()
    connection.execute("PRAGMA foreign_keys = ON")


_SENSITIVE_KEY = re.compile(r"(?:authorization|cookie|password|secret|token|api.?key)", re.I)
_HEADER_CARRY_RE = re.compile(
    r"(\b(?:x-)?api-?key\s*:|\bcookie\s*:|\bauthorization\s*:|\btoken\s*:)", re.I
)


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


def safe_text(text: str) -> str:
    """Free-text message safe for diagnostics/logs: header/credential-free.

    An error string may embed request headers ("X-apikey: ...", "Cookie: ...")
    whatever the source error wording; anything after such a marker is never
    operator-facing, so the whole header run is replaced with a marker.
    """
    scrubbed = _HEADER_CARRY_RE.sub(r"\1[redacted]", text)
    if scrubbed != text:
        scrubbed = "[redacted] (transport message withheld: may embed credentials)"
    return scrubbed


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
                # Diagnostics preserve the status code and retryability;
                # safe_record scrubs any sensitive keys before persistence.
                # The message itself may carry request headers (urllib error
                # strings embed them), so it goes through the same scrub.
                failure = source_http.failure_metadata(exc)
                _record_diagnostic(
                    database, run_id, retailer, catalog_id, "error",
                    level="error",
                    message=safe_text(str(exc)),
                    request_metadata={**metadata, **failure},
                )
                if attempt_number >= max_retries or not source_http.is_retryable_failure(exc):
                    raise
                delay = source_http.backoff_delay(
                    retry_backoff, attempt_number, getattr(exc, "retry_after", None)
                )
                _record_diagnostic(
                    database, run_id, retailer, catalog_id, "retry",
                    message=f"retrying after {delay:.1f}s",
                    request_metadata={**metadata, **failure, "delay_seconds": round(delay, 3)},
                )
                if delay:
                    time.sleep(delay)
        raise AssertionError("unreachable")

    def fetch(search_term: str) -> Mapping[str, Any]:
        return attempt(fetcher, search_term)

    if direct_fetcher is not None:
        setattr(fetch, "fetch_product", lambda tpnb: attempt(direct_fetcher, tpnb))
    return fetch


# Current Feed / Price Observation reads are re-exported from feed_reads
# (the single read interface) for api.py, dashboard_read.py, and tests.

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
        if not mapping.source_product_reference and not _name_matches(expected, name):
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


SUPERVALU_HOME = "https://shop.supervalu.ie/"
SUPERVALU_ENDPOINT = "https://storefrontgateway.supervalu.ie/api/stores/{store_id}/search"
SUPERVALU_PAGE_SIZE = 50  # the ``take`` bound requested from the gateway
SUPERVALU_PRODUCT_ENDPOINT = "https://storefrontgateway.supervalu.ie/api/stores/{store_id}/products/{product_id}"
_SUPERVALU_HEADERS = {
    "User-Agent": "drinks-tracker/0.1",
    "Origin": "https://shop.supervalu.ie",
    "Referer": SUPERVALU_HOME,
}


class SuperValuClient:
    """Fetch SuperValu's store-scoped search JSON with a storefront cookie."""

    def __init__(
        self,
        store_id: str,
        endpoint: str = SUPERVALU_ENDPOINT,
        opener: urllib.request.OpenerDirector | None = None,
        product_endpoint: str = SUPERVALU_PRODUCT_ENDPOINT,
        min_request_interval: float = 1.0,
    ):
        if not store_id.strip():
            raise ValueError("SuperValu store_id must not be empty")
        self.store_id = store_id
        self.endpoint = endpoint
        self.product_endpoint = product_endpoint
        self.opener = opener or urllib.request.build_opener(
            urllib.request.HTTPCookieProcessor(http.cookiejar.CookieJar())
        )
        self._transport = source_http.RetailerTransport(
            "SuperValu", opener=self.opener, min_request_interval=min_request_interval
        )
        self._session_ready = False

    def __call__(self, search_term: str) -> dict[str, Any]:
        if not self._session_ready:
            self._transport.text(SUPERVALU_HOME, accept="text/html", headers=_SUPERVALU_HEADERS)
            self._session_ready = True
        url = self.endpoint.format(store_id=urllib.parse.quote(self.store_id, safe=""))
        url += "?" + urllib.parse.urlencode({"q": search_term, "take": SUPERVALU_PAGE_SIZE})
        payload = self._transport.json(url, headers=_SUPERVALU_HEADERS)
        if not isinstance(payload, dict):
            raise RuntimeError("SuperValu response was not a JSON object")
        if not isinstance(payload.get("pagination"), Mapping) and payload.get("count") is None:
            # The gateway gave no completeness signal of its own: inject the
            # bounded page size requested so _page_completeness can compare
            # the returned count against it. A gateway-reported ``count`` is
            # left untouched — it is the stronger evidence.
            payload["pagination"] = {"pageSize": SUPERVALU_PAGE_SIZE}
        return payload

    def fetch_product(self, product_id: str) -> dict[str, Any]:
        """Hydrate one known product ID without repeating a catalog search."""
        if not str(product_id).strip():
            raise ValueError("SuperValu product_id must not be empty")
        url = self.product_endpoint.format(
            store_id=urllib.parse.quote(self.store_id, safe=""),
            product_id=urllib.parse.quote(str(product_id), safe=""),
        )
        payload = self._transport.json(url, headers=_SUPERVALU_HEADERS)
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

TESCO_SEARCH_ENDPOINT = "https://search.api.tesco.com/search"
TESCO_SEARCH_PAGE_SIZE = 10  # the ``count`` bound requested from the search API
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
        self.opener = opener
        self.min_request_interval = min_request_interval
        self._last_request_at: float | None = None
        self._transport = source_http.RetailerTransport(
            "Tesco", opener=opener or urllib.request.build_opener(),
            min_request_interval=min_request_interval,
        )
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
            {"distchannel": "ghs", "query": search_term, "count": TESCO_SEARCH_PAGE_SIZE, "geo": "ie"}
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
            products_node = search_payload["ie"]["ghs"]["products"]
            results = products_node["results"]
            tpnbs = [str(result["tpnb"]) for result in results if result.get("tpnb")]
        except (KeyError, TypeError, AttributeError) as exc:
            raise RuntimeError("Tesco search response has no product results") from exc
        if not isinstance(results, list):
            raise RuntimeError("Tesco search response has no product results")
        # Completeness evidence for _page_completeness: the raw search page
        # plus the total match count the response reports. When the total is
        # absent the bounded page size is the fallback proxy — fewer results
        # than the page size proves the source exhausted its matches; a page
        # at capacity may have been truncated.
        pagination: dict[str, Any] = {"pageSize": TESCO_SEARCH_PAGE_SIZE}
        total = products_node.get("total") if isinstance(products_node, dict) else None
        if isinstance(total, int) and not isinstance(total, bool):
            pagination["total"] = total
        if not tpnbs:
            return {
                "products": [],
                "search": search_payload,
                "items": results,
                "pagination": pagination,
            }
        return {
            "products": self._hydrate_tpnbs(tpnbs),
            "search": search_payload,
            "items": results,
            "pagination": pagination,
        }

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

    def _throttle(self) -> None:
        delay = source_http.spacing_delay(self._last_request_at, self.min_request_interval)
        if delay:
            time.sleep(delay)

    def _request_json(self, request: urllib.request.Request) -> Any:
        if self._impersonator is not None:
            # The impersonated session raises its own transport exception
            # types; anything raised here is an outage-class failure.
            self._throttle()
            try:
                response = self._impersonator.request(
                    request.get_method(),
                    request.full_url,
                    headers=dict(request.header_items()),
                    data=request.data,
                    timeout=30,
                )
                if response.status_code >= 400:
                    raise source_http.status_error(
                        "Tesco", response.status_code,
                        source_http.response_retry_after(response),
                    )
                return response.json()
            except source_http.SourceHTTPError:
                raise
            except Exception as exc:
                raise source_http.transport_error("Tesco", exc) from exc
            finally:
                self._last_request_at = time.monotonic()
        return self._transport.send(request)


@dataclass
class FetchOutcome:
    """What one fetch attempt produced, including how far it got.

    A fetch never raises past ``_collect_cell``'s single conversion point:
    an absence (LookupError) or failure lands in ``error`` while
    ``payload``/``hydrated``/``warnings`` keep whatever was achieved, so the
    shared cell lifecycle can classify and persist it.
    """

    payload: Mapping[str, Any] | None = None
    found: Any = None
    hydrated: bool = False
    warnings: list[tuple[str, str, str]] = field(default_factory=list)
    error: BaseException | None = None


@dataclass(frozen=True)
class RetailerExtraction:
    """The per-retailer variation of one Collection Result cell.

    ``_collect_cell`` owns the shared lifecycle — unmapped guard, absence
    classification, price math, persistence, diagnostics. Only what a
    retailer's source actually varies is encoded here.
    """

    retailer: str
    # -> mutates the FetchOutcome (payload/found/hydrated/warnings); raises
    #    on failure; absence is a LookupError.
    fetch: Callable[..., None]
    # Listing name persisted on the Price Observation.
    name: Callable[[Any], str]
    # -> (displayed_price, clubcard_price, drs_deposit). Raises ValueError
    #    on malformed evidence; the cell demotes that to source_error.
    prices: Callable[[Any], tuple[Decimal, Any, Any]]
    # -> (source_product_reference, source_item_id) for results rows.
    identity: Callable[[Any, Any], tuple[str, str]]
    # Mapping-row identity when it differs from the results identity.
    mapping_identity: Callable[[Any, Any], tuple[Any, Any]] | None = None
    # Name evidence the pack-validation guard checks (defaults to name).
    validate_name: Callable[[Any], str] | None = None
    # Source scope recorded on results/observations (SuperValu store id).
    scope: Callable[[Any], str | None] = lambda fetcher: None
    # Absence status when the page gives no completeness evidence.
    absence_unknown: Callable[[bool], str] = lambda hydrated: "not_found"
    # Harvest Catalog Candidates from a search payload (Dunnes only).
    candidates: Callable[[Mapping[str, Any], Any], list[tuple[Any, ...]]] | None = None
    # Warn when an observed price carries no DRS deposit evidence (Dunnes).
    drs_missing_warning: bool = False

    def validation_name(self, found: Any) -> str:
        return (
            self.validate_name(found) if self.validate_name is not None else self.name(found)
        )

    def mapping_identity_or_identity(self, found: Any, mapping: Any) -> tuple[Any, Any]:
        return (
            self.mapping_identity(found, mapping)
            if self.mapping_identity is not None
            else self.identity(found, mapping)
        )


def _outcome(fn: Callable[..., Any], *args: Any) -> FetchOutcome:
    """Run a fetch, converting every failure into ``FetchOutcome.error``."""
    outcome = FetchOutcome()
    try:
        fn(outcome, *args)
    except LookupError as exc:
        outcome.error = exc
    except Exception as exc:
        outcome.error = exc
    return outcome


def _direct_or_search_fetch(
    outcome: FetchOutcome,
    pack: BenchmarkPack,
    mapping: Any,
    fetcher: Callable[[str], Mapping[str, Any]],
    find: Callable[[Mapping[str, Any], Any], Any],
    source_id: str | None,
) -> None:
    """Search the catalog term, or hydrate one known source id directly.

    Direct hydration via the client's ``fetch_product`` answers for exactly
    one known product — no page window to truncate — so callers treat its
    absence as a definitive not_found.
    """
    direct_fetcher = getattr(fetcher, "fetch_product", None)
    hydrated = bool(source_id and callable(direct_fetcher))
    outcome.hydrated = hydrated
    payload = (
        direct_fetcher(str(source_id))
        if source_id and callable(direct_fetcher)
        else fetcher(pack.search_term)
    )
    outcome.payload = payload
    outcome.found = find(payload, mapping)


def _dunnes_fetch(
    outcome: FetchOutcome,
    pack: BenchmarkPack,
    mapping: DunnesMapping,
    fetcher: Callable[[str], Mapping[str, Any]],
) -> None:
    payload = fetcher(pack.search_term)
    complete = _page_completeness(payload)
    outcome.payload = payload
    try:
        outcome.found = _find_listing(payload, mapping)
    except LookupError:
        # The gateway's relevance is exact-substring: full pack names often
        # return zero results while the bare brand returns the whole range
        # (which contains the mapped item). Retry once with the brand term
        # before declaring absence.
        if complete != "true":
            raise
        payload = fetcher(pack.brand)
        outcome.payload = payload
        outcome.found = _find_listing(payload, mapping)


def _supervalu_fetch(
    outcome: FetchOutcome,
    pack: BenchmarkPack,
    mapping: SuperValuMapping,
    fetcher: Callable[[str], Mapping[str, Any]],
    hydrator: Callable[[str], Mapping[str, Any]] | None = None,
) -> None:
    # Direct hydration via stable source identifier where available.
    if mapping.source_product_id:
        hydrate = hydrator or getattr(fetcher, "fetch_product", None)
        if hydrate is not None and callable(hydrate):
            outcome.hydrated = True
            payload = hydrate(str(mapping.source_product_id))
            outcome.payload = payload
            items = payload.get("items")
            if isinstance(items, list) and items:
                item = items[0] if isinstance(items[0], dict) else None
            elif isinstance(payload, dict) and payload.get("productId"):
                item = payload
            else:
                item = None
            if item is None:
                raise LookupError(
                    f"SuperValu product {mapping.source_product_id} returned no item"
                )
            outcome.found = item
            return
    outcome.payload = fetcher(pack.search_term)
    outcome.found = _find_supervalu_listing(outcome.payload, mapping)


def _tesco_fetch(
    outcome: FetchOutcome,
    pack: BenchmarkPack,
    mapping: TescoMapping,
    fetcher: Callable[[str], Mapping[str, Any]],
) -> None:
    direct_fetcher = getattr(fetcher, "fetch_product", None)
    if mapping.source_tpnb and not callable(direct_fetcher):
        # Fixture case: direct TPNB hydration was expected for this mapping,
        # so record the search fallback once the run row exists.
        outcome.warnings.append((
            "collection_fallback", "warning",
            "direct TPNB hydration expected; falling back to search",
        ))
    _direct_or_search_fetch(
        outcome, pack, mapping, fetcher, _find_tesco_listing, mapping.source_tpnb
    )


def _lidl_fetch(
    outcome: FetchOutcome,
    pack: BenchmarkPack,
    mapping: LidlMapping,
    fetcher: Callable[[str], Mapping[str, Any]],
) -> None:
    _direct_or_search_fetch(
        outcome, pack, mapping, fetcher, _find_lidl_listing, mapping.source_product_id
    )


def _aldi_fetch(
    outcome: FetchOutcome,
    pack: BenchmarkPack,
    mapping: AldiMapping,
    fetcher: Callable[[str], Mapping[str, Any]],
) -> None:
    _direct_or_search_fetch(
        outcome, pack, mapping, fetcher, _find_aldi_listing, mapping.source_product_id
    )


_DUNNES = RetailerExtraction(
    retailer="dunnes",
    fetch=_dunnes_fetch,
    name=lambda found: found[0].get("productName", ""),
    prices=lambda found: (
        _decimal_price(found[2]["Price"]),
        None,
        _dunnes_drs_deposit(found[2]),
    ),
    identity=lambda found, mapping: (
        str(found[0].get("productReference") or "") if found else "",
        str(found[1].get("itemId") or "") if found else "",
    ),
    mapping_identity=lambda found, mapping: (
        mapping.source_product_reference, mapping.source_item_id,
    ),
    absence_unknown=lambda hydrated: "inconclusive",
    candidates=_candidate_products,
    drs_missing_warning=True,
)

_SUPERVALU = RetailerExtraction(
    retailer="supervalu",
    fetch=_supervalu_fetch,
    name=lambda found: found.get("name", ""),
    prices=lambda found: (
        _decimal_price(
            found.get("priceNumeric")
            if found.get("priceNumeric") is not None
            else found.get("price")
        ),
        _optional_price(
            found, "clubcardPrice", "clubCardPrice", "loyaltyPrice", "memberPrice"
        ),
        _supervalu_drs_deposit(found),
    ),
    identity=lambda found, mapping: (
        (
            str(found.get("productId") or found.get("sku") or "")
            if found
            else str(mapping.source_product_id or "")
        ),
        (
            str(found.get("sku") or found.get("productId") or "") if found else ""
        ),
    ),
    absence_unknown=lambda hydrated: "not_found" if hydrated else "inconclusive",
    scope=lambda fetcher: getattr(fetcher, "store_id", None),
)

_TESCO = RetailerExtraction(
    retailer="tesco",
    fetch=_tesco_fetch,
    name=lambda found: found.get("title", ""),
    prices=lambda found: (
        _decimal_price((found.get("price") or {}).get("actual")),
        _tesco_clubcard_price(found),
        _tesco_drs_deposit(found),
    ),
    identity=lambda found, mapping: (
        str(found.get("tpnb") or "") if found else str(mapping.source_tpnb or ""),
        (
            str(found.get("id") or found.get("gtin") or found.get("tpnb") or "")
            if found
            else ""
        ),
    ),
    absence_unknown=lambda hydrated: "not_found" if hydrated else "inconclusive",
)

_LIDL = RetailerExtraction(
    retailer="lidl",
    fetch=_lidl_fetch,
    name=lambda found: found.get("name", ""),
    prices=lambda found: (
        _decimal_price(found.get("price")), None, _lidl_drs_deposit(found),
    ),
    identity=lambda found, mapping: (
        str(found.get("productId") or "") if found else str(mapping.source_product_id or ""),
        str(found.get("productId") or "") if found else "",
    ),
)

_ALDI = RetailerExtraction(
    retailer="aldi",
    fetch=_aldi_fetch,
    name=lambda found: found.get("name", ""),
    prices=lambda found: (
        _decimal_price(found.get("price")), None, _aldi_drs_deposit(found),
    ),
    identity=lambda found, mapping: (
        str(found.get("productId") or "") if found else str(mapping.source_product_id or ""),
        str(found.get("productId") or "") if found else "",
    ),
    # Aldi keeps the brand in a structured field rather than the product
    # name, so validate against the combined evidence.
    validate_name=lambda found: " ".join(
        str(part) for part in (found.get("brand"), found.get("name")) if part
    ),
)


def _collect_cell(
    pack: BenchmarkPack,
    mapping: Any,
    fetcher: Callable[[str], Mapping[str, Any]],
    database: str | Path,
    extraction: RetailerExtraction,
    *,
    scope: str | None = None,
    _run_id: str | None = None,
    _started_at: str | None = None,
) -> dict[str, Any]:
    """Collect one mapped pack and return the operator-facing run summary.

    The deep Collection Result lifecycle shared by every retailer: unmapped
    guard, fetch + absence classification, Price Observation price math, and
    the full persistence block. Per-retailer variation lives in the
    ``RetailerExtraction`` adapter.
    """
    if pack.catalog_id != mapping.catalog_id:
        raise ValueError(
            f"catalog pack and {extraction.retailer} mapping must have the same catalog_id"
        )
    if pack.pack_count < 1 or pack.unit_size_ml < 1:
        raise ValueError("pack composition must contain positive count and size")

    started_at = _started_at or timestamp()
    started = time.monotonic()
    run_id = _run_id or uuid.uuid4().hex
    own_run = _run_id is None
    status = "observed"
    error: str | None = None
    outcome = FetchOutcome()
    complete = "unknown"

    if mapping.status != "approved":
        status = "unmapped"
        error = "catalog mapping is not approved"
    else:
        outcome = _outcome(extraction.fetch, pack, mapping, fetcher)
        complete = _page_completeness(outcome.payload)
        if outcome.error is None:
            reason = _validate_listing(extraction.validation_name(outcome.found), pack)
            if reason is not None:
                status = "source_error"
                error = f"stale source identifier: {reason}"
        elif isinstance(outcome.error, LookupError):
            # Absence from a page that provably covered every match is a
            # proven not_found; absence from a truncated or evidence-free
            # page is inconclusive (or per-retailer: a direct hydration has
            # no page window, so its absence stays definitive).
            status = _absence_status(
                outcome.payload,
                unknown_status=extraction.absence_unknown(outcome.hydrated),
            )
            error = str(outcome.error)
        else:
            status = "source_error"
            error = str(outcome.error)

    observed_at = timestamp()
    duration_ms = round((time.monotonic() - started) * 1000, 1)
    displayed_price: Decimal | None = None
    clubcard_price: Decimal | None = None
    drs_deposit: Decimal | None = None
    component_unit_price: str | None = None
    price_per_litre: str | None = None

    if status == "observed":
        assert outcome.found is not None
        try:
            displayed_price, clubcard_price, drs_deposit = extraction.prices(outcome.found)
            component_unit_price = _decimal_text(displayed_price / pack.pack_count)
            litres = Decimal(pack.pack_count * pack.unit_size_ml) / Decimal(1000)
            price_per_litre = _decimal_text(displayed_price / litres, "0.0001")
        except Exception as exc:
            # Malformed prices demote to source_error; never throw mid-observation.
            status = "source_error"
            error = str(exc)
            displayed_price = None
            clubcard_price = None
            drs_deposit = None
            component_unit_price = None
            price_per_litre = None

    result_ref, result_item = extraction.identity(outcome.found, mapping)
    map_ref, map_item = extraction.mapping_identity_or_identity(outcome.found, mapping)
    summary = {
        "run_id": run_id,
        "retailer": extraction.retailer,
        "catalog_id": pack.catalog_id,
        "status": status,
        "complete": complete,
        "observed_count": int(status == "observed"),
        "failed_count": int(status == "source_error"),
        "duration_ms": duration_ms,
    }
    if scope:
        summary["source_scope"] = scope

    database_path = Path(database)
    database_path.parent.mkdir(parents=True, exist_ok=True)
    with closing(sqlite3.connect(database_path)) as connection:
        ensure_schema(connection)
        upsert_catalog_pack(connection, pack)
        connection.execute(
            """
            INSERT INTO catalog_mappings
                (catalog_id, retailer, expected_product_name,
                 source_product_reference, source_item_id, status, approved_at)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(catalog_id, retailer) DO UPDATE SET
                expected_product_name=excluded.expected_product_name,
                source_product_reference=excluded.source_product_reference,
                source_item_id=excluded.source_item_id, status=excluded.status,
                approved_at=CASE
                    WHEN catalog_mappings.status = 'dormant' THEN excluded.approved_at
                    ELSE COALESCE(catalog_mappings.approved_at, excluded.approved_at)
                END
            """,
            (
                mapping.catalog_id, extraction.retailer,
                mapping.expected_product_name,
                map_ref or None, map_item or None, mapping.status, timestamp(),
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
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(run_id, catalog_id, retailer) DO UPDATE SET
                status=excluded.status, error=excluded.error,
                source_product_reference=excluded.source_product_reference,
                source_item_id=excluded.source_item_id,
                source_scope=excluded.source_scope,
                complete=excluded.complete, recorded_at=excluded.recorded_at
            """,
            (
                run_id, pack.catalog_id, extraction.retailer, status, error,
                result_ref or None, result_item or None, scope,
                complete, observed_at,
            ),
        )
        if outcome.payload is not None and extraction.candidates is not None:
            for reference, item_id, name, price, raw_record in extraction.candidates(
                outcome.payload, mapping
            ):
                candidate_id = f"{extraction.retailer}:{reference}:{item_id}"
                connection.execute(
                    """
                    INSERT INTO catalog_candidates (
                        candidate_id, retailer, source_product_reference, source_item_id,
                        source_product_name, displayed_price, raw_record, status, first_seen_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, 'pending_review', ?)
                    ON CONFLICT(candidate_id) DO UPDATE SET
                        displayed_price=excluded.displayed_price,
                        raw_record=excluded.raw_record
                    """,
                    (
                        candidate_id, extraction.retailer, reference, item_id,
                        name, price, raw_record, observed_at,
                    ),
                )
        if status == "observed":
            assert displayed_price is not None
            connection.execute(
                """
                INSERT INTO price_observations (
                    run_id, catalog_id, retailer, source_product_reference,
                    source_item_id, source_product_name, displayed_price, clubcard_price,
                    drs_deposit, source_scope, currency, pack_count, unit_size_ml,
                    package_type, component_unit_price, price_per_litre, observed_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'EUR', ?, ?, ?, ?, ?, ?)
                ON CONFLICT DO NOTHING
                """,
                (
                    run_id, pack.catalog_id, extraction.retailer,
                    result_ref, result_item, extraction.name(outcome.found),
                    _decimal_text(displayed_price),
                    _decimal_text(clubcard_price) if clubcard_price is not None else None,
                    _decimal_text(drs_deposit) if drs_deposit is not None else None,
                    scope, pack.pack_count, pack.unit_size_ml, pack.package_type,
                    component_unit_price, price_per_litre, observed_at,
                ),
            )
            _touch_mapping_last_observed(
                connection, pack.catalog_id, extraction.retailer, observed_at
            )
            if extraction.drs_missing_warning and drs_deposit is None:
                # Validated source limitation: no live VTEX offer carries
                # deposit evidence (see _dunnes_drs_deposit).
                connection.execute(
                    """
                    INSERT INTO collection_diagnostics
                        (run_id, retailer, catalog_id, level, event, message,
                         raw_record, request_metadata, created_at)
                    VALUES (?, ?, ?, 'warning', 'drs_not_available', ?, NULL, NULL, ?)
                    """,
                    (
                        run_id, extraction.retailer, pack.catalog_id,
                        f"{extraction.retailer} offer evidence exposes no DRS"
                        " deposit; stored as NULL",
                        timestamp(),
                    ),
                )
        for event, level, message in outcome.warnings:
            connection.execute(
                """
                INSERT INTO collection_diagnostics
                    (run_id, retailer, catalog_id, level, event, message,
                     raw_record, request_metadata, created_at)
                VALUES (?, ?, ?, ?, ?, ?, NULL, NULL, ?)
                """,
                (
                    run_id, extraction.retailer, pack.catalog_id,
                    level, event, message, timestamp(),
                ),
            )
        if status == "source_error" and outcome.payload is not None:
            # A drifted/failed listing keeps its raw record in diagnostics so
            # the operator can re-approve the identity (wrong-product audit).
            connection.execute(
                """
                INSERT INTO collection_diagnostics
                    (run_id, retailer, catalog_id, level, event, message,
                     raw_record, request_metadata, created_at)
                VALUES (?, ?, ?, 'error', 'result', ?, ?, NULL, ?)
                """,
                (
                    run_id, extraction.retailer, pack.catalog_id,
                    error, safe_record(outcome.payload), timestamp(),
                ),
            )
        connection.commit()
    return summary | ({"error": error} if error else {})


def collect_one(
    pack: BenchmarkPack,
    mapping: DunnesMapping,
    fetcher: Callable[[str], Mapping[str, Any]],
    database: str | Path,
    *,
    _run_id: str | None = None,
    _started_at: str | None = None,
) -> dict[str, Any]:
    """Collect one mapped Dunnes pack and return the operator-facing run summary."""
    return _collect_cell(
        pack, mapping, fetcher, database, _DUNNES,
        _run_id=_run_id, _started_at=_started_at,
    )


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
    store_id = store_id or getattr(fetcher, "store_id", None)
    if not store_id:
        raise ValueError("SuperValu store_id is required")
    extraction = _SUPERVALU
    if hydrator is not None:
        extraction = replace(
            _SUPERVALU,
            fetch=lambda outcome, pack_, mapping_, fetcher_: _supervalu_fetch(
                outcome, pack_, mapping_, fetcher_, hydrator=hydrator
            ),
        )
    return _collect_cell(
        pack, mapping, fetcher, database, extraction, scope=store_id,
        _run_id=_run_id, _started_at=_started_at,
    )


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
    return _collect_cell(
        pack, mapping, fetcher, database, _TESCO,
        _run_id=_run_id, _started_at=_started_at,
    )


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
    return _collect_cell(
        pack, mapping, fetcher, database, _LIDL,
        _run_id=_run_id, _started_at=_started_at,
    )


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
    return _collect_cell(
        pack, mapping, fetcher, database, _ALDI,
        _run_id=_run_id, _started_at=_started_at,
    )


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


def _touch_mapping_last_observed(
    connection: sqlite3.Connection,
    catalog_id: str,
    retailer: str,
    observed_at: str,
) -> None:
    """Stamp the mapping row with the latest successful observation time.

    Retention reconciles this from the observation store as well, so a
    mapping's staleness anchor stays truthful across source scopes and
    direct writers (e.g. basketwatch).
    """
    connection.execute(
        """
        UPDATE catalog_mappings SET last_observed_at = ?
        WHERE catalog_id = ? AND retailer = ?
          AND (last_observed_at IS NULL OR last_observed_at < ?)
        """,
        (observed_at, catalog_id, retailer, observed_at),
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
            ON CONFLICT(run_id, catalog_id, retailer) DO UPDATE SET
                status=excluded.status, error=excluded.error,
                source_scope=excluded.source_scope, recorded_at=excluded.recorded_at
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
    logger.log(level, safe_text(" ".join(fields)))


def _mapping_rows(value: Any) -> list[Any]:
    if value is None:
        return []
    if isinstance(value, Mapping):
        return list(value.values())
    return list(value)


def build_client(
    name: str,
    *,
    supervalu_store_id: str | None = None,
    focused: bool = False,
) -> Any:
    """Construct the retailer client for one retailer name.

    One registry for the three wiring sites (collection CLI, canary,
    discovery). Raises ``ValueError`` when credentials or configuration are
    missing, so callers can skip one retailer without blocking the others.
    ``focused=True`` selects the collection clients for Lidl/Aldi
    (``lidl.LidlClient``/``aldi.AldiClient`` with detail endpoints); the
    default returns the discovery search clients.
    """
    if name == "dunnes":
        return DunnesClient()
    if name == "supervalu":
        store_id = supervalu_store_id or os.environ.get("SUPERVALU_STORE_ID") or ""
        return SuperValuClient(store_id)
    if name == "tesco":
        return TescoClient()
    if name == "lidl":
        return WorkingLidlClient() if focused else LidlClient()
    if name == "aldi":
        return WorkingAldiClient() if focused else AldiClient()
    raise ValueError(f"unsupported retailer: {name}")


class _RunLock:
    """Advisory exclusive lock keeping local collectors single-file.

    The lock lives next to the database (``<database>.lock``) and is held via
    ``flock`` for the lifetime of a collection run, so a crashed process
    releases it automatically when the OS closes its descriptors. A second
    collector started while the lock is held fails fast instead of interleaving
    writes into the same feed.
    """

    def __init__(self, database: str | Path) -> None:
        self.path = Path(f"{database}.lock")
        self._fd: int | None = None

    def __enter__(self) -> _RunLock:
        fd = os.open(self.path, os.O_CREAT | os.O_RDWR, 0o644)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as exc:
            os.close(fd)
            raise RuntimeError(
                f"another collection run holds the lock at {self.path}; "
                "refusing to start a concurrent local collector"
            ) from exc
        self._fd = fd
        return self

    def __exit__(self, exc_type: Any, exc: Any, tb: Any) -> None:
        if self._fd is not None:
            fcntl.flock(self._fd, fcntl.LOCK_UN)
            os.close(self._fd)
            self._fd = None


def _finalize_interrupted_runs(connection: sqlite3.Connection) -> None:
    """Give runs left 'running' by a crashed collector a terminal status.

    Called when a new run starts: any prior run still marked 'running' can
    only come from a process that died mid-run, so it is finalized as
    'interrupted' with its finished_at and summary completed.
    """
    stale = connection.execute(
        "SELECT run_id, summary FROM collection_runs WHERE status = 'running'"
    ).fetchall()
    if not stale:
        return
    finalized_at = timestamp()
    for run_id, summary_text in stale:
        try:
            payload = json.loads(summary_text) if summary_text else {}
            if not isinstance(payload, dict):
                payload = {}
        except ValueError:
            payload = {}
        payload["status"] = "interrupted"
        payload["finished_at"] = finalized_at
        connection.execute(
            """
            UPDATE collection_runs
            SET status = 'interrupted', finished_at = ?, summary = ?
            WHERE run_id = ? AND status = 'running'
            """,
            (finalized_at, json.dumps(payload), run_id),
        )


def _fail_run(database: str | Path, run_id: str, status: str, error: str) -> None:
    """Finalize a run row that never reached its normal completion path.

    Best-effort: a failure here (e.g. the database itself is broken) must not
    mask the original exception that escaped the run.
    """
    try:
        with closing(sqlite3.connect(database)) as connection:
            ensure_schema(connection)
            connection.execute(
                """
                UPDATE collection_runs
                SET finished_at = ?, status = ?, summary = ?
                WHERE run_id = ? AND status = 'running'
                """,
                (timestamp(), status, json.dumps({"status": status, "error": error}), run_id),
            )
            connection.commit()
    except sqlite3.Error:
        logger.warning("could not finalize run %s as %s", run_id, status)


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
    circuit_threshold: int = source_http.DEFAULT_CIRCUIT_THRESHOLD,
    circuit_cooldown: float = source_http.DEFAULT_CIRCUIT_COOLDOWN,
) -> dict[str, Any]:
    """Run the active catalog matrix with isolated retailer-pack results."""
    if max_retries < 0 or retry_backoff < 0:
        raise ValueError("retry settings must not be negative")
    if circuit_threshold < 1 or circuit_cooldown < 0:
        raise ValueError("circuit settings must not be negative")

    started_at = timestamp()
    started = time.monotonic()
    run_id = uuid.uuid4().hex
    database_path = Path(database)
    database_path.parent.mkdir(parents=True, exist_ok=True)
    with _RunLock(database_path), closing(sqlite3.connect(database_path)) as connection:
        ensure_schema(connection)
        _finalize_interrupted_runs(connection)
        connection.execute(
            """
            INSERT INTO collection_runs
                (run_id, started_at, finished_at, status, observed_count, failed_count, summary)
            VALUES (?, ?, ?, 'running', 0, 0, ?)
            """,
            (run_id, started_at, started_at, json.dumps({"status": "running"})),
        )
        connection.commit()

    try:
        summary = _run_matrix(
            catalog=catalog,
            mappings=mappings,
            adapters=adapters,
            database_path=database_path,
            run_id=run_id,
            started_at=started_at,
            started=started,
            retailer=retailer,
            catalog_id=catalog_id,
            store_ids=store_ids,
            max_retries=max_retries,
            retry_backoff=retry_backoff,
            circuit_threshold=circuit_threshold,
            circuit_cooldown=circuit_cooldown,
        )
    except BaseException as exc:
        # A run that dies unexpectedly (process interruption, database error)
        # must never stay 'running'; finalize it and re-raise.
        _fail_run(
            database_path,
            run_id,
            "interrupted" if isinstance(exc, KeyboardInterrupt) else "failed",
            str(exc) or exc.__class__.__name__,
        )
        raise
    return summary


def _run_matrix(
    *,
    catalog: list[BenchmarkPack],
    mappings: Mapping[str, Any],
    adapters: Mapping[str, Callable[[str], Mapping[str, Any]]],
    database_path: Path,
    run_id: str,
    started_at: str,
    started: float,
    retailer: str | None,
    catalog_id: str | None,
    store_ids: Mapping[str, str] | None,
    max_retries: int,
    retry_backoff: float,
    circuit_threshold: int,
    circuit_cooldown: float,
) -> dict[str, Any]:
    """Collect the retailer-pack matrix for one prepared run row."""
    selected_catalog = [
        pack for pack in catalog
        if catalog_id is None or pack.catalog_id == catalog_id
    ]
    selected_retailers = [
        name for name in adapters
        if retailer is None or name == retailer
    ]
    store_ids = store_ids or {}

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

    for retailer_name in selected_retailers:
        rows = {
            row.catalog_id: row
            for row in _mapping_rows(mappings.get(retailer_name))
            if getattr(row, "catalog_id", None)
        }
        adapter = adapters[retailer_name]
        # One breaker per retailer: repeated consecutive source failures stop
        # further requests to that retailer for the rest of the run.
        breaker = source_http.CircuitBreaker(
            threshold=circuit_threshold, cooldown=circuit_cooldown
        )
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
                if breaker.open:
                    result = {
                        "status": "source_error",
                        "error": (
                            f"circuit open after {breaker.threshold} consecutive "
                            "failures; remaining requests to this retailer are "
                            "skipped for this run"
                        ),
                        "observed_count": 0,
                        "failed_count": 1,
                    }
                    _record_collection_result(
                        database_path, run_id, pack, retailer_name,
                        result["status"], result["error"], scope,
                    )
                    _record_diagnostic(
                        database_path, run_id, retailer_name, pack.catalog_id,
                        "circuit_open", level="error", message=result["error"],
                        request_metadata={"threshold": breaker.threshold},
                    )
                else:
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
                    # A half-open trial that fails re-trips the breaker; any
                    # non-error outcome (observed, not_found, inconclusive)
                    # resets the failure streak.
                    if result["status"] == "source_error":
                        breaker.record_failure()
                    else:
                        breaker.record_success()

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
    """Apply operational retention and mark or remove stale mappings.

    A mapping's staleness anchor is the newest Price Observation for its
    retailer-pack cell across every source scope; a mapping that has never
    produced an observation ages from its ``approved_at`` timestamp.  Only
    ``approved`` and ``dormant`` mappings transition (approved → dormant at
    ``dormant_days``, then purged with their observations at ``purge_days``).
    Catalog Candidates that are still open for review, or that a mapping or
    open discovery review still references, are preserved regardless of age.
    """
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
        # Catalog Candidates: keep anything still actionable — pending review
        # work, a candidate a mapping was approved from, or one an open
        # discovery review points at.  Everything older than the raw window
        # that no longer matters is dropped.
        candidate_delete = """
            DELETE FROM catalog_candidates
            WHERE first_seen_at < ?
              AND status <> 'pending_review'
        """
        candidate_parameters: list[str] = [raw_cutoff]
        # The candidate_id provenance column only exists once discovery's
        # schema has been applied to this database.
        if "candidate_id" in _columns_of(connection, "catalog_mappings"):
            candidate_delete += """
              AND candidate_id NOT IN (
                  SELECT candidate_id FROM catalog_mappings
                  WHERE candidate_id IS NOT NULL
              )
            """
        if _table_exists(connection, "discovery_cells"):
            candidate_delete += """
              AND NOT EXISTS (
                  SELECT 1 FROM discovery_cells AS dc
                  WHERE dc.candidate_id = catalog_candidates.candidate_id
                    AND dc.state = 'review'
              )
            """
        counts["deleted_candidates"] = connection.execute(
            candidate_delete, tuple(candidate_parameters)
        ).rowcount
        # Mappings approved through a path that does not stamp approved_at
        # (e.g. discovery decisions) age from the moment retention first
        # sees them, never from a fabricated earlier date.
        connection.execute(
            "UPDATE catalog_mappings SET approved_at = ? WHERE approved_at IS NULL",
            (_iso(current),),
        )
        # Reconcile last_observed_at with the actual observation store so the
        # anchor reflects every source scope, including direct writes that do
        # not go through the collector's mapping bookkeeping.
        if _has_observation_columns(connection):
            connection.execute(
                """
                UPDATE catalog_mappings AS cm
                SET last_observed_at = (
                    SELECT MAX(po.observed_at) FROM price_observations AS po
                    WHERE po.catalog_id = cm.catalog_id AND po.retailer = cm.retailer
                )
                WHERE EXISTS (
                    SELECT 1 FROM price_observations AS po
                    WHERE po.catalog_id = cm.catalog_id AND po.retailer = cm.retailer
                )
                """
            )
        # Approved mappings with no observation for dormant_days — including
        # mappings that never produced an observation — go dormant.
        counts["dormant_mappings"] = connection.execute(
            """
            UPDATE catalog_mappings
            SET status = 'dormant'
            WHERE status = 'approved'
              AND COALESCE(last_observed_at, approved_at) <= ?
            """,
            (dormant_cutoff,),
        ).rowcount
        # Dormant mappings past the purge window lose their detailed
        # observations (all source scopes) and the mapping row itself; the
        # Benchmark Catalog identity stays eligible for remapping.
        counts["purged_observations"] = connection.execute(
            """
            DELETE FROM price_observations
            WHERE EXISTS (
                SELECT 1 FROM catalog_mappings AS cm
                WHERE cm.catalog_id = price_observations.catalog_id
                  AND cm.retailer = price_observations.retailer
                  AND cm.status IN ('approved', 'dormant')
                  AND COALESCE(cm.last_observed_at, cm.approved_at) <= ?
            )
            """,
            (purge_cutoff,),
        ).rowcount
        counts["purged_mappings"] = connection.execute(
            """
            DELETE FROM catalog_mappings
            WHERE status IN ('approved', 'dormant')
              AND COALESCE(last_observed_at, approved_at) <= ?
            """,
            (purge_cutoff,),
        ).rowcount
        connection.commit()
    return counts


# Canonical status vocabularies enforced by check_integrity().
_RESULT_STATUSES = frozenset(
    {"observed", "not_found", "source_error", "unmapped", "inconclusive"}
)
_RUN_STATUSES = frozenset(
    {"completed", "failed", "interrupted", "running", "partial"}  # 'partial' is legacy
)
_MAPPING_STATUSES = frozenset({"approved", "review", "unmapped", "rejected", "dormant"})
_CANDIDATE_STATUSES = frozenset({"pending_review", "approved", "rejected", "resolved"})

_STATUS_CHECKS: tuple[tuple[str, frozenset[str]], ...] = (
    ("collection_results", _RESULT_STATUSES),
    ("collection_runs", _RUN_STATUSES),
    ("catalog_mappings", _MAPPING_STATUSES),
    ("catalog_candidates", _CANDIDATE_STATUSES),
)

# Money columns in price_observations must hold plain decimal text (as
# written by _decimal_text) or NULL — never floats, symbols, or prose.
_MONEY_PATTERN = re.compile(r"\d+(?:\.\d+)?\Z")
_MONEY_COLUMNS: tuple[str, ...] = (
    "displayed_price",
    "clubcard_price",
    "drs_deposit",
    "component_unit_price",
    "price_per_litre",
)


def _valid_money(value: Any) -> bool:
    """Whether ``value`` is a well-formed decimal money string (or empty)."""
    if value is None or value == "":
        return True
    return bool(_MONEY_PATTERN.fullmatch(str(value).strip()))


def check_integrity(database: str | Path) -> dict[str, Any]:
    """Run database-level integrity checks and report every violation class.

    Checks SQLite's structural integrity (``PRAGMA integrity_check``),
    foreign-key relationships (``PRAGMA foreign_key_check``), Collection
    Result / run / mapping / candidate status vocabularies, and the money
    text columns of Price Observations.  Read-only apart from any schema
    migration ensure_schema performs on a pre-versioning database.
    """
    with closing(sqlite3.connect(database)) as connection:
        ensure_schema(connection)
        integrity = connection.execute("PRAGMA integrity_check").fetchone()[0]
        foreign_key_violations = [
            {"table": row[0], "rowid": row[1], "parent": row[2], "fkid": row[3]}
            for row in connection.execute("PRAGMA foreign_key_check").fetchall()
        ]
        invalid_statuses: dict[str, int] = {}
        for table, statuses in _STATUS_CHECKS:
            if not _table_exists(connection, table):
                invalid_statuses[table] = 0
                continue
            placeholders = ", ".join("?" for _ in statuses)
            invalid_statuses[table] = connection.execute(
                f"SELECT COUNT(*) FROM {table} "
                f"WHERE status IS NULL OR status NOT IN ({placeholders})",
                tuple(sorted(statuses)),
            ).fetchone()[0]
        invalid_money_values: dict[str, int] = {}
        money_columns = tuple(
            name
            for name in _MONEY_COLUMNS
            if name in _columns_of(connection, "price_observations")
        )
        if money_columns:
            columns = ", ".join(money_columns)
            for row in connection.execute(
                f"SELECT {columns} FROM price_observations"
            ):
                for name, value in zip(money_columns, row):
                    if not _valid_money(value):
                        invalid_money_values[name] = (
                            invalid_money_values.get(name, 0) + 1
                        )
        for name in _MONEY_COLUMNS:
            invalid_money_values.setdefault(name, 0)
    violations = (
        integrity != "ok"
        or bool(foreign_key_violations)
        or any(invalid_statuses.values())
        or any(invalid_money_values.values())
    )
    return {
        "integrity_check": integrity,
        "foreign_key_violations": foreign_key_violations,
        "invalid_statuses": invalid_statuses,
        "invalid_money_values": invalid_money_values,
        "ok": not violations,
    }


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
    parser.add_argument(
        "--release-gate", action="store_true",
        help="refuse retailers whose live canary is failing (audit-10 release gate;"
             " also DRINKS_RELEASE_GATE=1)",
    )
    parser.add_argument(
        "--gate-state", type=Path, default=None,
        help="release-gate state file (default data/canary-gate.json or"
             " DRINKS_CANARY_STATE)",
    )
    args = parser.parse_args(argv)

    if not args.release_gate and os.environ.get("DRINKS_RELEASE_GATE", "").strip().lower() in {
        "1", "true", "yes",
    }:
        args.release_gate = True

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

    if args.release_gate:
        # Release gate (audit-10): collection refuses retailers whose live
        # canary keeps failing. Opt-in so scheduled CI collection (disposable
        # database, no canary state) is never surprised by it.
        from . import canary as canary_module

        gate_state = args.gate_state or canary_module.DEFAULT_GATE_STATE
        blocked = canary_module.release_gate(gate_state)
        for gated_retailer in list(configured_retailers):
            reason = blocked.get(gated_retailer)
            if reason:
                print(f"release gate blocks {gated_retailer}: {reason}", file=sys.stderr)
                configured_retailers.remove(gated_retailer)
        if not configured_retailers:
            parser.error(
                "release gate blocked every selected retailer;"
                " run `python -m beverage_feed canary` to re-check"
            )

    adapters: dict[str, Callable[[str], Mapping[str, Any]]] = {}
    for retailer in configured_retailers:
        try:
            adapters[retailer] = build_client(
                retailer, supervalu_store_id=args.supervalu_store_id, focused=True
            )
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
