"""Optional BasketWatch external-source ingest.

BasketWatch is an external price aggregator.  Ingested snapshots never
auto-approve anything: records matching an existing approved mapping become
Price Observations tagged ``source_scope='basketwatch'``, everything else is
queued as Catalog Candidates for operator review.  Collection
(:mod:`beverage_feed.collector`) remains the canonical observation writer;
this module appends secondary-source observations under the same rules
(append-only, Decimal money, ``timestamp()`` stamps, ``safe_record`` raw).
"""

from __future__ import annotations

import argparse
import json
import os
import sqlite3
import urllib.parse
import urllib.request
import uuid
from contextlib import closing
from decimal import Decimal
from pathlib import Path
from typing import Any, Callable, Mapping

from .collector import (
    BenchmarkPack,
    _decimal_price,
    _decimal_text,
    ensure_schema,
    safe_record,
    timestamp,
)
from .discovery_adapters import normalize_listing
from .matching import SourceListing, match_catalog

BASKETWATCH_ENDPOINT = "https://api.basketwatch.example/v1/snapshots"
# Assumption requiring live verification: the BasketWatch base URL is not
# published yet; override BASKETWATCH_ENDPOINT here or via the constructor.


def _load_catalog_packs(database: str | Path) -> list[BenchmarkPack]:
    database_path = Path(database)
    database_path.parent.mkdir(parents=True, exist_ok=True)
    with closing(sqlite3.connect(database_path)) as connection:
        ensure_schema(connection)
        connection.row_factory = sqlite3.Row
        return [
            BenchmarkPack(
                catalog_id=row["catalog_id"],
                name=row["name"],
                brand=row["brand"],
                variant=row["variant"],
                pack_count=row["pack_count"],
                unit_size_ml=row["unit_size_ml"],
                package_type=row["package_type"],
                search_term=row["search_term"],
            )
            for row in connection.execute("SELECT * FROM catalog_packs").fetchall()
        ]


class BasketWatchClient:
    """Authenticated client for one retailer's daily BasketWatch snapshot."""

    def __init__(
        self,
        api_key: str,
        endpoint: str = BASKETWATCH_ENDPOINT,
        opener: urllib.request.OpenerDirector | None = None,
    ):
        if not str(api_key).strip():
            raise ValueError("BasketWatch API key must not be empty")
        self.api_key = api_key
        self.endpoint = endpoint.rstrip("/")
        self.opener = opener or urllib.request.build_opener()

    def fetch_all(self, retailer_slug: str) -> list[dict[str, Any]]:
        """Fetch a daily source snapshot for one retailer."""
        if not retailer_slug.strip():
            raise ValueError("retailer_slug must not be empty")
        url = self.endpoint + "?" + urllib.parse.urlencode({"retailer": retailer_slug})
        request = urllib.request.Request(
            url,
            headers={
                "Accept": "application/json",
                "Authorization": f"Bearer {self.api_key}",
                "User-Agent": "drinks-tracker/0.1",
            },
        )
        try:
            with self.opener.open(request, timeout=30) as response:
                if getattr(response, "status", 200) >= 400:
                    raise RuntimeError(f"BasketWatch HTTP {response.status}")
                payload = json.load(response)
        except Exception as exc:
            if isinstance(exc, RuntimeError):
                raise
            raise RuntimeError(f"BasketWatch request failed: {exc}") from exc
        if isinstance(payload, dict):
            payload = payload.get("records")
        if not isinstance(payload, list):
            raise RuntimeError("BasketWatch snapshot was not a record list")
        return [record for record in payload if isinstance(record, dict)]


def _approved_mapping_cells(
    database: str | Path, retailer: str
) -> dict[str, dict[str, Any]]:
    """Approved mappings for one retailer, keyed by catalog_id."""
    database_path = Path(database)
    database_path.parent.mkdir(parents=True, exist_ok=True)
    with closing(sqlite3.connect(database_path)) as connection:
        ensure_schema(connection)
        connection.row_factory = sqlite3.Row
        return {
            row["catalog_id"]: dict(row)
            for row in connection.execute(
                "SELECT * FROM catalog_mappings WHERE retailer=? AND status='approved'",
                (retailer,),
            ).fetchall()
        }


def ingest_basketwatch_snapshot(
    retailer_slug: str,
    database: str | Path,
    api_key: str,
    *,
    client_factory: Callable[[str], Any] | None = None,
) -> dict[str, Any]:
    """Ingest matched BasketWatch records and queue candidates for review."""
    if not str(api_key).strip():
        raise EnvironmentError(
            "BASKETWATCH_API_KEY is required for BasketWatch ingest;"
            " set it in the environment or .env"
        )
    factory = client_factory or BasketWatchClient
    records = factory(api_key).fetch_all(retailer_slug)
    catalog = _load_catalog_packs(database)
    approved = _approved_mapping_cells(database, retailer_slug)

    run_id = uuid.uuid4().hex
    started_at = timestamp()
    summary: dict[str, Any] = {
        "run_id": run_id,
        "retailer": retailer_slug,
        "fetched": len(records),
        "ingested": 0,
        "queued_candidates": 0,
        "skipped_unmapped": 0,
        "skipped_invalid_price": 0,
    }

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
        try:
            summary = _ingest_records(
                connection, records, catalog, approved, retailer_slug, run_id, summary,
            )
        except Exception as exc:
            finished_at = timestamp()
            connection.execute(
                """
                UPDATE collection_runs
                SET finished_at = ?, status = 'failed', observed_count = ?,
                    failed_count = 1, summary = ?
                WHERE run_id = ?
                """,
                (
                    finished_at,
                    summary["ingested"],
                    json.dumps({**summary, "error": str(exc)}),
                    run_id,
                ),
            )
            connection.commit()
            raise
        finished_at = timestamp()
        connection.execute(
            """
            UPDATE collection_runs
            SET finished_at = ?, status = 'completed', observed_count = ?, summary = ?
            WHERE run_id = ?
            """,
            (finished_at, summary["ingested"], json.dumps(summary), run_id),
        )
        connection.commit()
    return summary


def _ingest_records(
    connection: sqlite3.Connection,
    records: list[dict[str, Any]],
    catalog: list[BenchmarkPack],
    approved: dict[str, dict[str, Any]],
    retailer_slug: str,
    run_id: str,
    summary: dict[str, Any],
) -> dict[str, Any]:
    """Persist one BasketWatch snapshot into observations and candidates."""
    now = timestamp()
    for record in records:
        listing = normalize_listing(retailer_slug, record)
        match = match_catalog(catalog, SourceListing(
            retailer=retailer_slug,
            source_product_reference=listing.source_identity,
            source_item_id=listing.source_identity,
            name=listing.name,
            brand=listing.attributes.get("brand"),
            variant=listing.attributes.get("variant"),
            pack_count=listing.attributes.get("pack_count"),
            unit_size_ml=listing.attributes.get("unit_size_ml"),
            package_type=listing.attributes.get("package_type"),
        ))
        approved_cell = (
            match.status == "approved" and match.catalog_id in approved
        )
        if not approved_cell:
            # Never auto-approve: queue every unmatched record for review.
            connection.execute(
                """
                INSERT INTO catalog_candidates (
                    candidate_id, retailer, source_product_reference,
                    source_item_id, source_product_name, displayed_price,
                    raw_record, status, first_seen_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, 'pending_review', ?)
                ON CONFLICT(candidate_id) DO UPDATE SET
                    displayed_price=excluded.displayed_price,
                    raw_record=excluded.raw_record
                """,
                (
                    f"{retailer_slug}:{listing.source_identity}",
                    retailer_slug,
                    listing.source_identity,
                    listing.source_identity,
                    listing.name,
                    (
                        _decimal_text(_decimal_price(listing.price.raw_value))
                        if listing.price.status == "valid"
                        else None
                    ),
                    safe_record(record),
                    now,
                ),
            )
            summary["skipped_unmapped"] += 1
            summary["queued_candidates"] += 1
            continue
        if listing.price.status != "valid":
            summary["skipped_invalid_price"] += 1
            continue
        pack = next(p for p in catalog if p.catalog_id == match.catalog_id)
        displayed_price = _decimal_price(listing.price.raw_value)
        litres = Decimal(pack.pack_count * pack.unit_size_ml) / Decimal(1000)
        connection.execute(
            """
            INSERT INTO price_observations (
                run_id, catalog_id, retailer, source_product_reference,
                source_item_id, source_product_name, displayed_price,
                drs_deposit, source_scope, currency, pack_count,
                unit_size_ml, package_type, component_unit_price,
                price_per_litre, observed_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, NULL, 'basketwatch', 'EUR', ?, ?, ?, ?, ?, ?)
            """,
            (
                run_id,
                pack.catalog_id,
                retailer_slug,
                listing.source_identity,
                listing.source_identity,
                listing.name,
                _decimal_text(displayed_price),
                pack.pack_count,
                pack.unit_size_ml,
                pack.package_type,
                _decimal_text(displayed_price / pack.pack_count),
                _decimal_text(displayed_price / litres, "0.0001"),
                timestamp(),
            ),
        )
        connection.execute(
            """
            INSERT INTO collection_results
                (run_id, catalog_id, retailer, status, error,
                 source_product_reference, source_item_id, source_scope, recorded_at)
            VALUES (?, ?, ?, 'observed', NULL, ?, NULL, 'basketwatch', ?)
            """,
            (run_id, pack.catalog_id, retailer_slug,
             listing.source_identity, timestamp()),
        )
        summary["ingested"] += 1
    return summary


def main(argv: list[str] | None = None) -> int:
    """CLI entrypoint for one-off BasketWatch ingest."""
    parser = argparse.ArgumentParser(description="Ingest one BasketWatch snapshot")
    parser.add_argument("--database", type=Path,
                        default=Path(os.environ.get("DRINKS_DATABASE", "data/feed.sqlite")))
    parser.add_argument("--retailer", required=True)
    args = parser.parse_args(argv)

    api_key = os.environ.get("BASKETWATCH_API_KEY", "")
    summary = ingest_basketwatch_snapshot(args.retailer, args.database, api_key)
    print(
        f"basketwatch ingest: run={summary['run_id']} "
        f"fetched={summary['fetched']} ingested={summary['ingested']} "
        f"queued_candidates={summary['queued_candidates']} "
        f"skipped_unmapped={summary['skipped_unmapped']} "
        f"skipped_invalid_price={summary['skipped_invalid_price']}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
