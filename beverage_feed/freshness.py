"""Feed freshness snapshot — the passive liveness signal (audit R5).

Prints the age of the freshest Price Observation per retailer so staleness
is visible whenever anyone looks at the logs. Purely passive: always exits 0,
no alerts, no services. Read-only: opens the feed database ``mode=ro`` and
never creates or migrates it.
"""

from __future__ import annotations

import argparse
import os
import sqlite3
from contextlib import closing
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .collector import as_datetime

_KNOWN_RETAILERS_SQL = "SELECT DISTINCT retailer FROM catalog_mappings ORDER BY retailer"
_FRESHEST_SQL = (
    "SELECT MAX(observed_at) FROM price_observations WHERE retailer = ?"
)


def freshness_snapshot(database: str | Path) -> list[dict[str, Any]]:
    """Return one row per mapped retailer: freshest observation age in days.

    Retailers with mappings but no observations report ``None`` — that is the
    frozen-collection signature (supervalu/tesco, 2026-08-27 → 2026-09-03).
    """
    with closing(
        sqlite3.connect(f"file:{database}?mode=ro", uri=True)
    ) as connection:
        retailers = [row[0] for row in connection.execute(_KNOWN_RETAILERS_SQL)]
        rows = []
        for retailer in retailers:
            (freshest,) = connection.execute(
                _FRESHEST_SQL, (retailer,)
            ).fetchone()
            age_days: float | None = None
            if freshest is not None:
                delta = (
                    datetime.now(timezone.utc) - as_datetime(freshest)
                ).total_seconds()
                age_days = round(delta / 86400, 1)
            rows.append(
                {
                    "retailer": retailer,
                    "freshest_observation": freshest,
                    "age_days": age_days,
                }
            )
    return rows


def main(argv: list[str] | None = None) -> int:
    """CLI: print a compact one-line freshness summary."""
    parser = argparse.ArgumentParser(
        description="Print freshest observation age per retailer (passive)"
    )
    parser.add_argument(
        "--database",
        default=os.environ.get("DRINKS_DATABASE", "data/feed.sqlite"),
    )
    args = parser.parse_args(argv)
    rows = freshness_snapshot(args.database)
    stale = max(
        (row["age_days"] for row in rows if row["age_days"] is not None),
        default=None,
    )
    never = [row["retailer"] for row in rows if row["age_days"] is None]
    parts = [f"retailers={len(rows)}"]
    parts.append(
        f"freshest_max_age_days={stale}" if stale is not None else "no observations"
    )
    if never:
        parts.append(f"never_observed={','.join(never)}")
    print("freshness " + " ".join(parts))
    return 0
