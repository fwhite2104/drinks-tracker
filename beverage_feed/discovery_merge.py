"""Surgical merge of a CI discovery database into a live feed database.

Copies only ``discovery_*`` rows plus ``catalog_candidates`` (the candidate
registry review decisions validate against) missing from the target,
deduplicated by natural keys (run_id / attempt_id / candidate_id /
retailer+catalog_id).  Collection result tables (observations, collection
results, catalog mappings) are never touched, so a merge never loses fresher
collection data.
"""

from __future__ import annotations

import argparse
import sqlite3
from contextlib import closing
from pathlib import Path
from typing import Any

from .discovery import ensure_discovery_schema

# Natural key per discovery table, in dependency order (runs before attempts
# before everything that references them).  Auto-increment primary keys are
# excluded: the target assigns fresh ids, dedupe is by the natural key only.
_NATURAL_KEYS: dict[str, tuple[str, ...]] = {
    "discovery_runs": ("run_id",),
    "discovery_attempts": ("attempt_id",),
    "catalog_candidates": ("candidate_id",),
    "discovery_cells": ("retailer", "catalog_id"),
    "discovery_candidate_cells": ("candidate_id", "retailer", "catalog_id"),
    "discovery_candidate_search_terms": ("candidate_id", "retailer", "search_term"),
    "discovery_search_history": (
        "run_id", "attempt_id", "retailer", "catalog_id",
        "search_term", "searched_at", "request_kind",
    ),
    # ponytail: evidence key is the full row (minus surrogate id) because two
    # evidence records can share candidate/cell/recorded_at with different
    # attributes; exact-content dedupe is the only lossless natural key.
    "discovery_candidate_evidence": (
        "candidate_id", "retailer", "catalog_id", "recorded_at",
        "raw_attributes", "normalized_attributes", "inference_basis",
        "attribute_diffs", "raw_price_value", "price_parse_status",
        "price_parse_reason",
    ),
    "discovery_state_transitions": (
        "retailer", "catalog_id", "to_state", "changed_at", "changed_by",
    ),
    "discovery_identity_links": (
        "retailer", "weak_candidate_id", "strong_candidate_id", "relationship",
    ),
    "discovery_rejections": ("section", "canonical_key", "rejected_at"),
    # ponytail: discovery_diagnostics skipped — run telemetry with no natural
    # key; add a full-tuple key here if diagnostics ever become review evidence.
}

# Surrogate auto-increment columns per table, dropped on copy so the target
# assigns fresh ids.  catalog_candidates carries none: candidate_id is the key.
_SURROGATE_IDS = ("search_id", "evidence_id", "transition_id", "link_id")


def merge_discovery_database(source: str | Path, target: str | Path) -> dict[str, int]:
    """Copy missing discovery rows from ``source`` into ``target``.

    Returns the number of rows inserted per table.  Idempotent: a second run
    over the same pair inserts nothing.  Existing target rows always win —
    a merge never overwrites a mapping decision already recorded in the VM.
    """
    source_path = Path(source)
    if not source_path.is_file():
        raise ValueError(f"source database not found: {source_path}")
    ensure_discovery_schema(target)
    inserted: dict[str, int] = {}
    with (
        closing(sqlite3.connect(f"file:{source_path}?mode=ro", uri=True)) as src,
        closing(sqlite3.connect(target)) as dst,
    ):
        dst.execute("PRAGMA foreign_keys = ON")
        for table, key_columns in _NATURAL_KEYS.items():
            existing = {
                tuple(row)
                for row in dst.execute(
                    f"SELECT {', '.join(key_columns)} FROM {table}"  # noqa: S608
                )
            }
            columns = [
                row[1] for row in src.execute(f"PRAGMA table_info({table})")
            ]
            autoincrement = "AUTOINCREMENT" in (
                src.execute(
                    "SELECT sql FROM sqlite_master WHERE type='table' AND name=?",
                    (table,),
                ).fetchone()[0]
                or ""
            )
            if autoincrement:
                # Drop the surrogate id; the target assigns its own.
                columns = [
                    column for column in columns if column not in _SURROGATE_IDS
                ]
            copied = 0
            for row in src.execute(
                f"SELECT {', '.join(columns)} FROM {table}"  # noqa: S608
            ):
                values = dict(zip(columns, row))
                key = tuple(values[column] for column in key_columns)
                if key in existing:
                    continue
                placeholders = ", ".join("?" for _ in columns)
                dst.execute(
                    f"INSERT INTO {table} ({', '.join(columns)}) "  # noqa: S608
                    f"VALUES ({placeholders})",
                    row,
                )
                existing.add(key)
                copied += 1
            inserted[table] = copied
        dst.commit()
    return inserted


def main(argv: list[str] | None = None) -> int:
    """CLI: merge a rediscovery artifact database into the live feed DB."""
    parser = argparse.ArgumentParser(
        description="Merge missing discovery_* rows from a source database "
        "into the live feed database without touching collection tables.",
    )
    parser.add_argument("--source", required=True, help="path to the source (CI artifact) sqlite DB")
    parser.add_argument(
        "--target", default="data/feed.sqlite",
        help="live feed database (default: data/feed.sqlite)",
    )
    args = parser.parse_args(argv)
    counts: dict[str, Any] = merge_discovery_database(args.source, args.target)
    total = sum(counts.values())
    detail = ", ".join(f"{table}={n}" for table, n in counts.items() if n)
    print(f"merged {total} rows into {args.target}" + (f" ({detail})" if detail else " (nothing new)"))
    return 0
