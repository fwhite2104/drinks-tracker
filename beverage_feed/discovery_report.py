"""Discovery coverage and review-backlog reporting.

Reports mapping decisions and backlog only.  Pending, inconclusive,
identity-unstable, and do-not-map cells are work states — never retailer
availability or stock claims.
"""

from __future__ import annotations

import argparse
import json
import os
import sqlite3
from contextlib import closing
from datetime import timedelta
from pathlib import Path
from typing import Any

from .collector import as_datetime, timestamp
from .discovery import DiscoveryStore, load_mappings, reconcile_json_decisions

RETAILERS = ("dunnes", "supervalu", "tesco", "lidl", "aldi")
_CELL_COUNTS = (
    "approved", "review", "review_missing", "review_conflicting",
    "review_conflicting_candidates", "review_challenge", "unmapped", "pending",
    "inconclusive", "identity_unstable", "rejected", "do_not_map",
)
_PRICE_STATUSES = ("valid", "missing", "malformed", "unsupported_promotion")
AGE_BUCKETS = ((0, 7, "0-7d"), (7, 30, "7-30d"), (30, None, ">30d"))


def _empty_row(name: str, total_cells: int) -> dict[str, Any]:
    row: dict[str, Any] = {"retailer": name, "total_cells": total_cells}
    for key in _CELL_COUNTS:
        row[key] = 0
    row.update({
        "active": total_cells,
        "eligible": 0,
        "coverage": 0.0,
        "inconclusive_rate": 0.0,
        "disagreement_rate": 0.0,
        "price_statuses": {status: 0 for status in _PRICE_STATUSES},
        "review_age_buckets": {label: 0 for _, _, label in AGE_BUCKETS},
        "auto_approved_tiers": {},
        "first_time_auto_approved": 0,
        "first_time_eligible_decisions": 0,
        "auto_approval_rate": 0.0,
    })
    return row


def _review_key(category: str | None) -> str:
    return {
        "missing": "review_missing",
        "conflicting": "review_conflicting",
        "conflicting-candidates": "review_conflicting_candidates",
        "challenge": "review_challenge",
    }.get(category or "", "review")


def _rate(numerator: int, denominator: int) -> float:
    return round(numerator / denominator, 4) if denominator else 0.0


# Metrics derived by formula rather than summed from per-retailer rows.
_COMPUTED_METRICS = {"active", "eligible", "coverage", "inconclusive_rate",
                     "auto_approval_rate", "disagreement_rate"}
# Metrics pre-set at initialization; don't accumulate from per-retailer rows.
_PRESET_METRICS = {"total_cells"}


def _accumulate_overall(overall: dict[str, Any], row: dict[str, Any]) -> None:
    """Add *row*'s raw counts into *overall*.

    Computed metrics (rates and denominators) are skipped — they are
    recalculated from overall totals after all rows have been accumulated.
    """
    for metric, value in row.items():
        if metric in _COMPUTED_METRICS or metric in _PRESET_METRICS:
            continue
        if isinstance(value, int):
            overall[metric] += value
        elif isinstance(value, dict):
            for sub_key, count in value.items():
                overall[metric][sub_key] = overall[metric].get(sub_key, 0) + count


def coverage_report(
    store: DiscoveryStore,
    *,
    catalog_count: int,
    mapping_path: str | Path | None = None,
    retailers: tuple[str, ...] = RETAILERS,
    now: str | None = None,
) -> dict[str, Any]:
    """Per-retailer and overall coverage metrics for the discovery matrix."""
    now = now or timestamp()
    rows = {name: _empty_row(name, catalog_count) for name in retailers}
    overall = _empty_row("overall", catalog_count * len(retailers))

    with closing(store.connection()) as connection:
        connection.row_factory = sqlite3.Row
        for cell in connection.execute(
            "SELECT retailer, state, review_category FROM discovery_cells"
        ):
            row = rows.get(cell["retailer"])
            if row is None:
                continue
            if cell["state"] == "review":
                row[_review_key(cell["review_category"])] += 1
                row["review"] += 1
            elif cell["state"] in _CELL_COUNTS:
                row[cell["state"]] += 1

        # Review-queue age buckets.
        for cell in connection.execute(
            "SELECT retailer, decided_at FROM discovery_cells WHERE state='review'"
        ):
            row = rows.get(cell["retailer"])
            if row is None or not cell["decided_at"]:
                continue
            age_days = (as_datetime(now) - as_datetime(cell["decided_at"])).days
            for low, high, label in AGE_BUCKETS:
                if age_days >= low and (high is None or age_days < high):
                    row["review_age_buckets"][label] += 1
                    break

        # Structured-vs-name disagreement rate from evidence diffs.
        evidence_totals: dict[str, tuple[int, int]] = {}
        for evidence in connection.execute(
            "SELECT retailer, COUNT(*) AS total, "
            "SUM(CASE WHEN attribute_diffs NOT IN ('', '{}') THEN 1 ELSE 0 END) AS diffs "
            "FROM discovery_candidate_evidence GROUP BY retailer"
        ):
            evidence_totals[evidence["retailer"]] = (evidence["total"], evidence["diffs"] or 0)
            row = rows.get(evidence["retailer"])
            if row is not None and evidence["total"]:
                row["disagreement_rate"] = _rate(evidence["diffs"] or 0, evidence["total"])

        # Candidate price evidence parse statuses (evidence only, never prices).
        for evidence in connection.execute(
            "SELECT retailer, price_parse_status, COUNT(*) AS total "
            "FROM discovery_candidate_evidence GROUP BY retailer, price_parse_status"
        ):
            row = rows.get(evidence["retailer"])
            status = evidence["price_parse_status"] or "missing"
            if row is not None and status in row["price_statuses"]:
                row["price_statuses"][status] += evidence["total"]

        # First-time terminal decisions: the earliest terminal transition per
        # cell, so repeat mature runs never inflate the auto-approval rate.
        transitions = connection.execute(
            "SELECT retailer, catalog_id, to_state, changed_by FROM "
            "discovery_state_transitions ORDER BY transition_id"
        ).fetchall()
    first_terminal: dict[tuple[str, str], sqlite3.Row] = {}
    for transition in transitions:
        if transition["to_state"] not in {"approved", "unmapped", "rejected"}:
            continue
        key = (transition["retailer"], transition["catalog_id"])
        first_terminal.setdefault(key, transition)

    first_time_auto: dict[str, int] = {name: 0 for name in retailers}
    first_time_eligible: dict[str, int] = {name: 0 for name in retailers}
    for (retailer, _), transition in first_terminal.items():
        if retailer not in first_time_eligible:
            continue
        first_time_eligible[retailer] += 1
        if transition["to_state"] == "approved" and transition["changed_by"] == "discovery":
            first_time_auto[retailer] += 1

    # Auto-approved identity tiers from the durable mapping file.
    if mapping_path is not None and Path(mapping_path).exists():
        for retailer, rows_ in load_mappings(mapping_path).items():
            row = rows.get(retailer)
            if row is None:
                continue
            for mapping in rows_:
                if mapping["status"] == "approved" and mapping.get("auto_approved"):
                    tier = mapping.get("identity_tier") or "unknown"
                    row["auto_approved_tiers"][tier] = row["auto_approved_tiers"].get(tier, 0) + 1

    request_counts: dict[str, int] = {}
    cells_advanced = 0
    with closing(store.connection()) as connection:
        connection.row_factory = sqlite3.Row
        for run in connection.execute("SELECT request_counts, cells_advanced FROM discovery_runs"):
            cells_advanced += run["cells_advanced"]
            for kind, count in json.loads(run["request_counts"] or "{}").items():
                request_counts[kind] = request_counts.get(kind, 0) + count

    for name, row in rows.items():
        row["active"] = row["total_cells"] - row["do_not_map"]
        row["eligible"] = row["approved"] + row["unmapped"] + row["rejected"]
        row["coverage"] = _rate(row["approved"], row["active"])
        row["inconclusive_rate"] = _rate(row["inconclusive"], row["active"])
        row["first_time_auto_approved"] = first_time_auto.get(name, 0)
        row["first_time_eligible_decisions"] = first_time_eligible.get(name, 0)
        row["auto_approval_rate"] = _rate(
            row["first_time_auto_approved"], row["first_time_eligible_decisions"]
        )
        _accumulate_overall(overall, row)
    overall["active"] = overall["total_cells"] - overall["do_not_map"]
    overall["eligible"] = overall["approved"] + overall["unmapped"] + overall["rejected"]
    overall["coverage"] = _rate(overall["approved"], overall["active"])
    overall["inconclusive_rate"] = _rate(overall["inconclusive"], overall["active"])
    overall["disagreement_rate"] = _rate(
        sum(diffs for _, diffs in evidence_totals.values()),
        sum(total for total, _ in evidence_totals.values()),
    )
    overall["auto_approval_rate"] = _rate(
        overall["first_time_auto_approved"], overall["first_time_eligible_decisions"]
    )

    return {
        "generated_at": now,
        "per_retailer": [rows[name] for name in retailers],
        "overall": overall,
        "requests_consumed": request_counts,
        "cells_advanced": cells_advanced,
    }


def format_report(report: dict[str, Any]) -> str:
    lines = [f"discovery coverage report generated={report['generated_at']}"]
    header = (
        "retailer total active approved coverage review missing conflicting "
        "conflicting-candidates challenge unmapped pending inconclusive "
        "identity-unstable do-not-map eligible auto-rate inconclusive-rate"
    )
    lines.append(header)
    for row in (*report["per_retailer"], report["overall"]):
        lines.append(
            f"{row['retailer']} {row['total_cells']} {row['active']} {row['approved']} "
            f"{row['coverage']:.3f} {row['review']} {row['review_missing']} "
            f"{row['review_conflicting']} {row['review_conflicting_candidates']} "
            f"{row['review_challenge']} {row['unmapped']} {row['pending']} "
            f"{row['inconclusive']} {row['identity_unstable']} {row['do_not_map']} "
            f"{row['eligible']} {row['auto_approval_rate']:.3f} {row['inconclusive_rate']:.3f}"
        )
    requests = ",".join(f"{kind}={count}" for kind, count in sorted(report["requests_consumed"].items())) or "-"
    lines.append(f"requests_consumed={requests} cells_advanced={report['cells_advanced']}")
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Report Catalog Mapping discovery coverage")
    parser.add_argument(
        "--database",
        type=Path,
        default=Path(os.environ.get("DRINKS_DATABASE", "data/feed.sqlite")),
    )
    parser.add_argument("--catalog", type=Path, default=Path("data/catalog.json"))
    parser.add_argument("--mapping", type=Path, default=Path("data/mappings.json"))
    parser.add_argument("--rejections", type=Path, default=Path("data/rejections.json"))
    parser.add_argument("--json", action="store_true", help="print JSON instead of a table")
    args = parser.parse_args(argv)

    from .collector import load_catalog, upsert_catalog_pack

    catalog = load_catalog(args.catalog)
    store = DiscoveryStore(args.database)
    with closing(store.connection()) as connection:
        for pack in catalog:
            upsert_catalog_pack(connection, pack)
        connection.commit()
    reconcile_json_decisions(store.database, args.mapping, args.rejections)
    report = coverage_report(store, catalog_count=len(catalog), mapping_path=args.mapping)
    print(json.dumps(report, indent=2) if args.json else format_report(report))
    return 0


__all__ = ["coverage_report", "format_report", "main"]
