"""Read-only HTTP API over the local price feed.

Local/internal only: no authentication and no write endpoints.  All data is
served from the SQLite database resolved from ``DRINKS_DATABASE``.
"""

from __future__ import annotations

import os
import sqlite3
from contextlib import asynccontextmanager, closing
from datetime import timedelta
from pathlib import Path
from typing import Any, AsyncIterator

from fastapi import FastAPI, HTTPException, Query

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


def _read_rows(database: Path, query: str) -> list[dict[str, Any]]:
    with closing(sqlite3.connect(database)) as connection:
        connection.row_factory = sqlite3.Row
        return [dict(row) for row in connection.execute(query).fetchall()]


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
    """API status, active database path, and total observation count."""
    rows = _read_rows(
        app.state.database, "SELECT COUNT(*) AS count FROM price_observations"
    )
    return {
        "status": "ok",
        "database": str(app.state.database),
        "observations": rows[0]["count"],
    }


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
