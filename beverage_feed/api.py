"""Read-only HTTP API over the local price feed.

Local/internal only: no authentication and no write endpoints.  All data is
served from the SQLite database resolved from ``DRINKS_DATABASE``.
"""

from __future__ import annotations

import json
import os
import sqlite3
from contextlib import asynccontextmanager, closing
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, AsyncIterator

from fastapi import FastAPI, HTTPException, Query

from . import collector
from .collector import (
    as_datetime,
    current_feed,
    ensure_schema,
    last_seen,
    price_history,
    timestamp,
)

_DEFAULT_DATABASE = "data/feed.sqlite"
_FRESHNESS_DAYS = 7


def _database_path() -> Path:
    return Path(os.environ.get("DRINKS_DATABASE", _DEFAULT_DATABASE))


@asynccontextmanager
async def lifespan(application: FastAPI) -> AsyncIterator[None]:
    """Resolve the database, migrate the schema, fail fast if unusable."""
    path = _database_path()
    try:
        with closing(sqlite3.connect(path)) as connection:
            ensure_schema(connection)
            connection.execute("SELECT 1 FROM price_observations").fetchone()
    except sqlite3.Error as exc:
        raise RuntimeError(
            f"cannot open price feed database at {path}: {exc}"
        ) from exc
    application.state.database = path
    yield


app = FastAPI(
    title="drinks-tracker",
    description="Read-only Irish grocery beverage price feed.",
    version="0.1.0",
    lifespan=lifespan,
)


def _read_rows(
    database: Path, query: str, parameters: tuple[Any, ...] = ()
) -> list[dict[str, Any]]:
    with closing(sqlite3.connect(database)) as connection:
        connection.row_factory = sqlite3.Row
        return [
            dict(row)
            for row in connection.execute(query, parameters).fetchall()
        ]


@app.get("/catalog")
def catalog() -> list[dict[str, Any]]:
    """All Benchmark Catalog rows."""
    return _read_rows(
        app.state.database,
        """
        SELECT catalog_id, name, brand, variant, pack_count,
               unit_size_ml, package_type, search_term
        FROM catalog_packs
        ORDER BY catalog_id
        """,
    )


@app.get("/prices/current")
def prices_current(
    retailer: str | None = Query(default=None),
    catalog_id: str | None = Query(default=None),
) -> list[dict[str, Any]]:
    """Latest successfully observed prices per retailer-pack pair."""
    return current_feed(app.state.database, retailer=retailer, catalog_id=catalog_id)


@app.get("/prices/history")
def prices_history(
    catalog_id: str = Query(),
    retailer: str | None = Query(default=None),
) -> list[dict[str, Any]]:
    """Append-only Price Observations for one pack, newest first."""
    return price_history(app.state.database, retailer=retailer, catalog_id=catalog_id)


@app.get("/last-seen")
def last_seen_for(
    retailer: str = Query(),
    catalog_id: str = Query(),
) -> dict[str, Any]:
    """Latest successful observation for one retailer-pack pair."""
    observation = last_seen(app.state.database, retailer=retailer, catalog_id=catalog_id)
    if observation is None:
        raise HTTPException(status_code=404, detail="pair has never been observed")
    return observation


@app.get("/health")
def health() -> dict[str, Any]:
    """API status, active database path, and table counts.

    ``code_mtime`` reflects when the running beverage_feed code was last
    changed (build time inside containers). If a freshly collected run still
    behaves like old code, compare ``code_mtime`` against the latest commit:
    a stale container image is the usual culprit.
    """
    counts = _read_rows(
        app.state.database,
        """
        SELECT
            (SELECT COUNT(*) FROM price_observations) AS observations,
            (SELECT COUNT(*) FROM collection_results) AS collection_results,
            (SELECT COUNT(*) FROM catalog_candidates) AS candidates,
            (SELECT COUNT(*) FROM catalog_mappings WHERE status = 'approved')
                AS approved_mappings,
            (SELECT COUNT(*) FROM catalog_packs) AS catalog_packs
        """,
    )[0]
    return {
        "status": "ok",
        "database": str(app.state.database),
        "code_mtime": _code_mtime(),
        **counts,
    }


def _code_mtime() -> str | None:
    """Modification time of the running collector module, ISO-8601 UTC."""
    path = Path(collector.__file__ or "collector.py")
    try:
        mtime = path.stat().st_mtime
    except OSError:
        return None
    return datetime.fromtimestamp(mtime, tz=timezone.utc).isoformat()


@app.get("/runs")
def runs(
    limit: int = Query(default=20, ge=1, le=200),
) -> list[dict[str, Any]]:
    """Recent collection runs with their operator-facing summaries."""
    rows = _read_rows(
        app.state.database,
        """
        SELECT run_id, started_at, finished_at, status,
               observed_count, failed_count, summary
        FROM collection_runs
        ORDER BY started_at DESC, rowid DESC
        LIMIT ?
        """,
        (limit,),
    )
    for row in rows:
        try:
            row["summary"] = json.loads(row["summary"])
        except (TypeError, json.JSONDecodeError):
            pass
    return rows


@app.get("/results")
def results(
    retailer: str | None = Query(default=None),
    status: str | None = Query(default=None),
    run_id: str | None = Query(default=None),
    limit: int = Query(default=500, ge=1, le=5000),
) -> list[dict[str, Any]]:
    """Raw collection results: every retailer-pack decision, no curation.

    Unlike ``/prices/current`` this includes ``unmapped``, ``not_found`` and
    ``source_error`` rows, so a scraped-but-dropped product is visible here
    with the reason it was dropped.
    """
    clauses = ["1=1"]
    parameters: list[Any] = []
    if retailer:
        clauses.append("cr.retailer = ?")
        parameters.append(retailer)
    if status:
        clauses.append("cr.status = ?")
        parameters.append(status)
    if run_id:
        clauses.append("cr.run_id = ?")
        parameters.append(run_id)
    parameters.append(limit)
    return _read_rows(
        app.state.database,
        f"""
        SELECT cr.run_id, cr.catalog_id, cp.name AS pack_name, cr.retailer,
               cr.status, cr.error, cr.source_product_reference,
               cr.source_item_id, cr.source_scope, cr.recorded_at
        FROM collection_results AS cr
        LEFT JOIN catalog_packs AS cp ON cp.catalog_id = cr.catalog_id
        WHERE {" AND ".join(clauses)}
        ORDER BY cr.recorded_at DESC, cr.rowid DESC
        LIMIT ?
        """,
        tuple(parameters),
    )


@app.get("/candidates")
def candidates(
    retailer: str | None = Query(default=None),
    status: str | None = Query(default=None),
    limit: int = Query(default=500, ge=1, le=5000),
) -> list[dict[str, Any]]:
    """Raw retailer listings captured during collection (Catalog Candidates).

    These are products seen in retailer searches that are not yet tied to a
    Benchmark Catalog pack. They never appear in ``/prices/current``; this
    endpoint is the no-frills view of everything ingestion has seen.
    """
    clauses = ["1=1"]
    parameters: list[Any] = []
    if retailer:
        clauses.append("retailer = ?")
        parameters.append(retailer)
    if status:
        clauses.append("status = ?")
        parameters.append(status)
    parameters.append(limit)
    return _read_rows(
        app.state.database,
        f"""
        SELECT candidate_id, retailer, source_product_reference, source_item_id,
               source_product_name, displayed_price, status, first_seen_at
        FROM catalog_candidates
        WHERE {" AND ".join(clauses)}
        ORDER BY first_seen_at DESC, rowid DESC
        LIMIT ?
        """,
        tuple(parameters),
    )


@app.get("/coverage")
def coverage() -> dict[str, Any]:
    """Mapping approval and recent-observation coverage per retailer cell."""
    cells = _read_rows(
        app.state.database,
        """
        SELECT cm.retailer, cm.catalog_id, cm.status AS mapping_status,
               MAX(po.observed_at) AS last_observed_at,
               COUNT(po.observation_id) AS observation_count
        FROM catalog_mappings AS cm
        LEFT JOIN price_observations AS po
          ON po.catalog_id = cm.catalog_id AND po.retailer = cm.retailer
        GROUP BY cm.retailer, cm.catalog_id
        ORDER BY cm.retailer, cm.catalog_id
        """,
    )
    cutoff = as_datetime(timestamp()) - timedelta(days=_FRESHNESS_DAYS)
    summaries: dict[str, dict[str, Any]] = {}
    for cell in cells:
        observed_at = cell["last_observed_at"]
        cell["fresh"] = bool(observed_at) and as_datetime(observed_at) >= cutoff
        summary = summaries.setdefault(cell["retailer"], {
            "retailer": cell["retailer"],
            "cells": 0,
            "approved": 0,
            "review": 0,
            "fresh_observations": 0,
        })
        summary["cells"] += 1
        if cell["mapping_status"] == "approved":
            summary["approved"] += 1
        elif cell["mapping_status"] == "review":
            summary["review"] += 1
        if cell["fresh"]:
            summary["fresh_observations"] += 1
    return {
        "generated_at": timestamp(),
        "freshness_days": _FRESHNESS_DAYS,
        "per_retailer": sorted(summaries.values(), key=lambda row: row["retailer"]),
        "cells": cells,
    }
