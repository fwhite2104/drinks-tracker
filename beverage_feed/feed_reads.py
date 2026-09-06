"""Read-only feed reads: the single interface for Current Feed queries.

Owns the read-only SQLite open (``mode=ro`` — never creates or migrates) and
the Current Feed / Price Observation SQL that every read surface (collector
queries, ``api.py``, both dashboards) shares. One implementation of the
Current Feed definition, so it cannot drift between surfaces.
"""

from __future__ import annotations

import sqlite3
from contextlib import closing
from pathlib import Path
from typing import Any

__all__ = ["open_readonly", "current_feed", "price_history", "last_seen"]


def open_readonly(database: str | Path) -> sqlite3.Connection:
    """Open an existing SQLite database read-only; never create or migrate."""
    uri = Path(database).resolve().as_uri() + "?mode=ro"
    connection = sqlite3.connect(uri, uri=True)
    connection.row_factory = sqlite3.Row
    return connection


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


def _table_exists(connection: sqlite3.Connection, name: str) -> bool:
    return connection.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (name,)
    ).fetchone() is not None


def _observation_projection(
    connection: sqlite3.Connection,
) -> tuple[str, str]:
    """Observation columns + catalog_packs join for databases without packs."""
    has_packs = _table_exists(connection, "catalog_packs")
    columns = (
        _OBSERVATION_COLUMNS
        if has_packs
        else _OBSERVATION_COLUMNS.replace(
            "cp.name AS catalog_name", "NULL AS catalog_name"
        )
    )
    pack_join = (
        "LEFT JOIN catalog_packs AS cp ON cp.catalog_id = po.catalog_id"
        if has_packs
        else ""
    )
    return columns, pack_join


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
    return (" AND " + " AND ".join(filters)) if filters else "", tuple(parameters)


def current_feed(
    database: str | Path,
    *,
    retailer: str | None = None,
    catalog_id: str | None = None,
) -> list[dict[str, Any]]:
    """Return only observed results from the latest result for each pair.

    The latest result wins even when it is ``not_found``, ``source_error``,
    or ``inconclusive``; this prevents an older price from being presented as
    current. Results for other retailer-pack pairs are independent. Tolerant
    of discovery-only databases: absent tables read as an empty feed.
    """
    with closing(open_readonly(database)) as connection:
        if not (
            Path(database).is_file()
            and _table_exists(connection, "collection_results")
            and _table_exists(connection, "price_observations")
        ):
            return []
        dormant_clause = (
            """
            AND NOT EXISTS (
                SELECT 1 FROM catalog_mappings AS cm
                WHERE cm.catalog_id = po.catalog_id
                  AND cm.retailer = po.retailer
                  AND cm.status = 'dormant'
            )
            """
            if _table_exists(connection, "catalog_mappings")
            else ""
        )
        columns, pack_join = _observation_projection(connection)
        filters, parameters = _filter_clause(retailer, catalog_id, "cr.")
        ordering = "cp.name" if pack_join else "po.catalog_id"
        rows = connection.execute(
            f"""
            WITH latest_results AS (
                SELECT cr.*,
                       ROW_NUMBER() OVER (
                           PARTITION BY cr.retailer, cr.catalog_id
                           ORDER BY cr.recorded_at DESC, cr.rowid DESC
                       ) AS position
                FROM collection_results AS cr
                WHERE 1=1{filters}
            ),
            winning_results AS (
                SELECT lr.run_id, lr.catalog_id, lr.retailer
                FROM latest_results AS lr
                WHERE lr.position = 1 AND lr.status = 'observed'
            ),
            ranked_observations AS (
                SELECT po.*,
                       ROW_NUMBER() OVER (
                           PARTITION BY po.run_id, po.retailer, po.catalog_id
                           ORDER BY po.observed_at DESC, po.observation_id DESC
                       ) AS obs_position
                FROM price_observations AS po
                JOIN winning_results AS wr
                  ON wr.run_id = po.run_id
                 AND wr.catalog_id = po.catalog_id
                 AND wr.retailer = po.retailer
            )
            SELECT {columns}
            FROM ranked_observations AS po
            {pack_join}
            WHERE po.obs_position = 1
              {dormant_clause}
            ORDER BY po.retailer, {ordering}, po.catalog_id
            """,
            parameters,
        ).fetchall()
        return [dict(row) for row in rows]


def price_history(
    database: str | Path,
    *,
    retailer: str | None = None,
    catalog_id: str | None = None,
) -> list[dict[str, Any]]:
    """Return append-only Price Observations, newest first."""
    if not Path(database).is_file():
        return []
    with closing(open_readonly(database)) as connection:
        if not _table_exists(connection, "price_observations"):
            return []
        has_mappings = _table_exists(connection, "catalog_mappings")
        columns, pack_join = _observation_projection(connection)
        mapping_join = (
            "LEFT JOIN catalog_mappings AS cm"
            " ON cm.catalog_id = po.catalog_id AND cm.retailer = po.retailer"
            if has_mappings
            else ""
        )
        dormant_filter = (
            "AND (cm.status IS NULL OR cm.status <> 'dormant')" if has_mappings else ""
        )
        filters, parameters = _filter_clause(retailer, catalog_id, "po.")
        rows = connection.execute(
            f"""
            SELECT {columns}
            FROM price_observations AS po
            {pack_join}
            {mapping_join}
            WHERE 1=1 {dormant_filter}{filters}
            ORDER BY po.observed_at DESC, po.observation_id DESC
            """,
            parameters,
        ).fetchall()
        return [dict(row) for row in rows]


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
