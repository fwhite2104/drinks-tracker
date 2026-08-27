"""Trace one product through the ingestion pipeline, read-only.

Answers "why is this product missing from the API/dashboard?" entirely from
what is already persisted in SQLite. No scraping happens here.

For a given ``--catalog-id`` (or ``--reference`` matching a source product
reference or item id) the tracer prints, per retailer:

1. Catalog Mapping state (approved / review / dormant / absent) — collection
   only attempts cells with an approved mapping.
2. Recent collection results (observed / not_found / source_error / unmapped)
   with the exact error text recorded at collection time.
3. Price Observations (what the curated API/dashboard views are built from).
4. Catalog Candidates — raw listings captured from retailer searches that are
   not tied to a pack; a product "found in scraping" but invisible elsewhere
   lives here.
5. Collection diagnostics recorded for the cell.

Usage::

    python -m beverage_feed trace --catalog-id coca-diet-330-8
    python -m beverage_feed trace --reference 3029607
    python -m beverage_feed trace --catalog-id coca-diet-330-8 --retailer tesco
"""

from __future__ import annotations

import argparse
import os
import sqlite3
from contextlib import closing
from pathlib import Path
from typing import Any

_DEFAULT_DATABASE = "data/feed.sqlite"
_RESULT_LIMIT = 8


def _rows(
    connection: sqlite3.Connection, query: str, parameters: tuple[Any, ...] = ()
) -> list[dict[str, Any]]:
    connection.row_factory = sqlite3.Row
    return [dict(row) for row in connection.execute(query, parameters).fetchall()]


def _print_section(title: str, rows: list[dict[str, Any]]) -> None:
    print(f"  {title}:")
    if not rows:
        print("    (none)")
        return
    for row in rows:
        parts = [
            f"{key}={value}"
            for key, value in row.items()
            if value is not None and value != ""
        ]
        print("    " + " ".join(parts))


def _identity_matches(row: dict[str, Any], reference: str) -> bool:
    fields = (
        row.get("source_product_reference"),
        row.get("source_item_id"),
        row.get("candidate_id"),
        row.get("matched_source_identity"),
    )
    return any(value and reference in str(value) for value in fields)


def trace(
    database: str | Path,
    *,
    catalog_id: str | None = None,
    reference: str | None = None,
    retailer: str | None = None,
) -> int:
    """Print every persisted stage for the traced product. Returns exit code."""
    with closing(sqlite3.connect(f"file:{database}?mode=ro", uri=True)) as connection:
        if reference and not catalog_id:
            # Resolve the reference to catalog cell(s) it has been seen with.
            cells = {
                row["catalog_id"]
                for row in _rows(
                    connection,
                    """
                    SELECT DISTINCT catalog_id FROM collection_results
                    WHERE source_product_reference = ? OR source_item_id = ?
                    """,
                    (reference, reference),
                )
            } | {
                row["catalog_id"]
                for row in _rows(
                    connection,
                    """
                    SELECT DISTINCT cm.catalog_id FROM catalog_mappings AS cm
                    WHERE cm.source_product_reference = ?
                       OR cm.source_item_id = ?
                    """,
                    (reference, reference),
                )
            }
            if not cells:
                print(f"no catalog cell references {reference!r}; "
                      "the product may only exist in catalog_candidates:")
                candidates = _rows(
                    connection,
                    """
                    SELECT candidate_id, retailer, source_product_name,
                           displayed_price, status, first_seen_at
                    FROM catalog_candidates
                    WHERE source_product_reference = ? OR source_item_id = ?
                    """,
                    (reference, reference),
                )
                _print_section("candidates", candidates)
                return 0 if candidates else 1
            catalog_id = sorted(cells)[0]
            if len(cells) > 1:
                print(f"reference {reference!r} maps to {sorted(cells)}; "
                      f"tracing {catalog_id}")
        if not catalog_id:
            print("provide --catalog-id or --reference")
            return 2

        print(f"tracing catalog_id={catalog_id}"
              + (f" reference={reference}" if reference else "")
              + (f" retailer={retailer}" if retailer else ""))
        print(f"database={database}\n")

        _print_section(
            "catalog pack",
            _rows(
                connection,
                """
                SELECT catalog_id, name, brand, variant, pack_count,
                       unit_size_ml, package_type, search_term
                FROM catalog_packs WHERE catalog_id = ?
                """,
                (catalog_id,),
            ),
        )

        mapping_where = "cm.catalog_id = ?"
        mapping_params: tuple[str, ...] = (catalog_id,)
        if retailer:
            mapping_where += " AND cm.retailer = ?"
            mapping_params = (*mapping_params, retailer)
        _print_section(
            "mappings (collection only runs approved cells)",
            _rows(
                connection,
                f"""
                SELECT cm.retailer, cm.status, cm.expected_product_name,
                       cm.source_product_reference, cm.source_item_id
                FROM catalog_mappings AS cm WHERE {mapping_where}
                """,
                mapping_params,
            ),
        )

        results_where = "catalog_id = ?"
        results_params: tuple[str, ...] = (catalog_id,)
        if retailer:
            results_where += " AND retailer = ?"
            results_params = (*results_params, retailer)
        _print_section(
            f"recent collection results (last {_RESULT_LIMIT})",
            _rows(
                connection,
                f"""
                SELECT cr.retailer, cr.status, cr.error, cr.recorded_at,
                       cr.run_id, cr.source_product_reference, cr.source_item_id
                FROM collection_results AS cr
                WHERE {results_where}
                ORDER BY cr.recorded_at DESC, cr.rowid DESC
                LIMIT {_RESULT_LIMIT}
                """,
                results_params,
            ),
        )

        _print_section(
            "price observations (curated views show these)",
            _rows(
                connection,
                f"""
                SELECT po.retailer, po.displayed_price, po.observed_at, po.run_id
                FROM price_observations AS po
                WHERE {results_where}
                ORDER BY po.observed_at DESC
                LIMIT {_RESULT_LIMIT}
                """,
                results_params,
            ),
        )

        _print_section(
            "collection diagnostics",
            _rows(
                connection,
                f"""
                SELECT d.retailer, d.level, d.event, d.message, d.created_at
                FROM collection_diagnostics AS d
                WHERE {results_where}
                ORDER BY d.created_at DESC
                LIMIT {_RESULT_LIMIT}
                """,
                results_params,
            ),
        )

        candidate_sql = """
            SELECT candidate_id, retailer, source_product_name,
                   displayed_price, status, first_seen_at
            FROM catalog_candidates WHERE catalog_candidates.retailer IN (
                SELECT DISTINCT retailer FROM collection_results
                WHERE catalog_id = ?
            )
        """
        candidate_params: list[Any] = [catalog_id]
        if retailer:
            candidate_sql = """
                SELECT candidate_id, retailer, source_product_name,
                       displayed_price, status, first_seen_at
                FROM catalog_candidates WHERE retailer = ?
            """
            candidate_params = [retailer]
        candidates = _rows(connection, candidate_sql, tuple(candidate_params))
        if reference:
            candidates = [row for row in candidates if _identity_matches(row, reference)]
        _print_section(
            "catalog candidates (raw scraped listings, uncurated)",
            candidates[:_RESULT_LIMIT],
        )
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Trace one product through the persisted pipeline stages",
    )
    parser.add_argument("--catalog-id", help="stable catalog_id to trace")
    parser.add_argument(
        "--reference",
        help="source product reference / item id / candidate id substring",
    )
    parser.add_argument("--retailer", help="restrict to one retailer")
    parser.add_argument(
        "--database",
        type=Path,
        default=Path(os.environ.get("DRINKS_DATABASE", _DEFAULT_DATABASE)),
    )
    args = parser.parse_args(argv)
    if not args.catalog_id and not args.reference:
        parser.error("one of --catalog-id or --reference is required")
    if not args.database.is_file():
        print(f"database not found: {args.database}")
        return 2
    try:
        return trace(
            args.database,
            catalog_id=args.catalog_id,
            reference=args.reference,
            retailer=args.retailer,
        )
    except sqlite3.Error as exc:
        print(f"cannot read {args.database}: {exc}")
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
