"""Catalog comparability scorecard: rank Benchmark Catalog packs by cross-retailer evidence.

Read-only analysis (full-feed-coverage step 2, Option B): joins the curated
catalog against ``discovery_candidate_cells``, ``catalog_mappings`` and
``discovery_rejections`` in the feed database to answer "how many distinct
retailers have evidence for this exact pack?". Writes only the markdown
report given by ``--output``; never edits ``data/catalog.json``,
``data/mappings.json`` or the database. The catalog swap it proposes is
gated on human review.
"""

from __future__ import annotations

import argparse
import sqlite3
from contextlib import closing
from pathlib import Path
from typing import Any

from .collector import load_catalog, timestamp

# A pack is "comparable" when two or more retailers have any evidence for it
# (candidates from discovery, or an approved mapping) — the minimum for the
# comparison screen to show more than one price. Packs below that are
# "single-retailer"; packs with no evidence at all are "no-signal".
COMPARABLE_RETAILERS = 2


def load_signals(database: str | Path) -> dict[str, dict[str, Any]]:
    """Per-catalog_id comparability signals from the discovery database.

    Returns ``{catalog_id: {"candidate_retailers": set, "candidates": int,
    "approved_retailers": set, "rejections": int}}``.
    """
    signals: dict[str, dict[str, Any]] = {}

    def _entry(catalog_id: str) -> dict[str, Any]:
        return signals.setdefault(catalog_id, {
            "candidate_retailers": set(),
            "candidates": 0,
            "approved_retailers": set(),
            "rejections": 0,
        })

    # Read-only URI (same seam as dashboard_read): mode=ro never creates or
    # writes the database, even when the path is wrong.
    with closing(sqlite3.connect(Path(database).resolve().as_uri() + "?mode=ro", uri=True)) as connection:
        for catalog_id, retailer, count in connection.execute(
            "SELECT catalog_id, retailer, COUNT(*) FROM discovery_candidate_cells "
            "GROUP BY catalog_id, retailer"
        ):
            entry = _entry(catalog_id)
            entry["candidate_retailers"].add(retailer)
            entry["candidates"] += count
        for catalog_id, retailer in connection.execute(
            "SELECT catalog_id, retailer FROM catalog_mappings WHERE status='approved'"
        ):
            _entry(catalog_id)["approved_retailers"].add(retailer)
        for catalog_id, count in connection.execute(
            "SELECT catalog_id, COUNT(*) FROM discovery_rejections "
            "WHERE section='listings' AND state='rejected' AND catalog_id IS NOT NULL "
            "GROUP BY catalog_id"
        ):
            _entry(catalog_id)["rejections"] += count
    return signals


def build_scorecard(catalog_path: str | Path, database: str | Path) -> list[dict[str, Any]]:
    """Score every catalog pack and return rows ranked by comparability."""
    signals = load_signals(database)
    rows: list[dict[str, Any]] = []
    for pack in load_catalog(Path(catalog_path)):
        signal = signals.get(pack.catalog_id, {})
        candidate_retailers = sorted(signal.get("candidate_retailers", set()))
        approved_retailers = sorted(signal.get("approved_retailers", set()))
        evidence_retailers = len(set(candidate_retailers) | set(approved_retailers))
        rows.append({
            "catalog_id": pack.catalog_id,
            "name": pack.name,
            "candidate_retailers": candidate_retailers,
            "approved_retailers": approved_retailers,
            "candidates": signal.get("candidates", 0),
            "rejections": signal.get("rejections", 0),
            "verdict": (
                "comparable" if evidence_retailers >= COMPARABLE_RETAILERS
                else "single-retailer" if evidence_retailers == 1
                else "no-signal"
            ),
        })
    rows.sort(key=_rank_key)
    for rank, row in enumerate(rows, start=1):
        row["rank"] = rank
    return rows


def _rank_key(row: dict[str, Any]) -> tuple[Any, ...]:
    """Rank by evidence breadth, then candidate volume, then rejection drag."""
    return (
        -len(row["approved_retailers"]),
        -len(row["candidate_retailers"]),
        -row["candidates"],
        row["rejections"],
        row["catalog_id"],
    )


def proposed_catalog(rows: list[dict[str, Any]]) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Split ranked rows into (keep, drop) for the proposed replacement catalog.

    Keep: every comparable pack (which includes the proven >=2-approved
    Coke packs). Drop: single-retailer rows (a screen with one price is not a
    comparison) and no-signal rows (no retailer has ever returned evidence).
    """
    return (
        [row for row in rows if row["verdict"] == "comparable"],
        [row for row in rows if row["verdict"] != "comparable"],
    )


def render_markdown(rows: list[dict[str, Any]], *, generated_at: str) -> str:
    """Render the scorecard report: ranked table, proposed catalog, drops."""
    keep, drop = proposed_catalog(rows)
    proven = [row for row in keep if len(row["approved_retailers"]) >= COMPARABLE_RETAILERS]
    lines = [
        "# Catalog comparability scorecard",
        "",
        f"Generated {generated_at} by `beverage_feed/scorecard.py` — read-only join of "
        "`data/catalog.json` × `discovery_candidate_cells` × `catalog_mappings` × "
        "`discovery_rejections`. No retailer requests, no catalog edits.",
        "",
        "Verdicts: **comparable** = ≥2 distinct retailers have any evidence "
        "(candidates or approved mappings); **single-retailer** = exactly 1; "
        "**no-signal** = none.",
        "",
        "## Headline",
        "",
        f"- Packs scored: **{len(rows)}**",
        f"- Comparable (≥2 retailers): **{len(keep)}** — of which "
        f"**{len(proven)}** proven with ≥2 approved mappings",
        f"- Single-retailer: **{sum(1 for r in rows if r['verdict'] == 'single-retailer')}**",
        f"- No-signal: **{sum(1 for r in rows if r['verdict'] == 'no-signal')}**",
        "",
        "The evidence in this database does not support 100 comparable packs. "
        "The proposed replacement catalog below is therefore smaller than 100; "
        "fill the remainder only after the Lidl/Aldi category walks (step 4) "
        "size what the discounters actually stock.",
        "",
        "## Ranked scorecard (all packs)",
        "",
        "| rank | pack | catalog_id | retailers w/ candidates | retailers approved | candidates | rejections | verdict |",
        "|---|---|---|---|---|---|---|---|",
    ]
    for row in rows:
        lines.append(
            f"| {row['rank']} | {row['name']} | {row['catalog_id']} "
            f"| {', '.join(row['candidate_retailers']) or '-'} "
            f"| {', '.join(row['approved_retailers']) or '-'} "
            f"| {row['candidates']} | {row['rejections']} | {row['verdict']} |"
        )
    lines += [
        "",
        "## Proposed catalog (replaces `data/catalog.json` after human review)",
        "",
        f"### Keep — {len(keep)} packs, ranked",
        "",
    ]
    for row in keep:
        lines.append(
            f"{row['rank']}. **{row['name']}** (`{row['catalog_id']}`) — "
            f"approved: {', '.join(row['approved_retailers']) or '-'}; "
            f"candidates: {', '.join(row['candidate_retailers']) or '-'}"
            + (" *(proven comparable)*" if len(row["approved_retailers"]) >= COMPARABLE_RETAILERS else "")
        )
    lines += [
        "",
        f"### Drop — {len(drop)} packs",
        "",
        "| pack | catalog_id | verdict | why |",
        "|---|---|---|---|",
    ]
    for row in drop:
        why = (
            "only one retailer has ever returned evidence — a screen with one "
            "price, not a comparison"
            if row["verdict"] == "single-retailer"
            else "no retailer has ever returned a candidate or mapping for this pack"
        )
        lines.append(f"| {row['name']} | {row['catalog_id']} | {row['verdict']} | {why} |")
    lines.append("")
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Score catalog packs by cross-retailer comparability (read-only)",
    )
    parser.add_argument("--catalog", type=Path, default=Path("data/catalog.json"))
    parser.add_argument("--database", type=Path, default=Path("data/feed.sqlite"))
    parser.add_argument(
        "--output", type=Path,
        default=Path(".scratch/full-feed-coverage/research/comparability-scorecard-2026-09-04.md"),
        help="markdown report destination (only file this tool writes)",
    )
    args = parser.parse_args(argv)

    rows = build_scorecard(args.catalog, args.database)
    report = render_markdown(rows, generated_at=timestamp())
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(report)
    keep = sum(1 for row in rows if row["verdict"] == "comparable")
    print(
        f"scorecard: packs={len(rows)} comparable={keep} "
        f"single-retailer={sum(1 for r in rows if r['verdict'] == 'single-retailer')} "
        f"no-signal={sum(1 for r in rows if r['verdict'] == 'no-signal')} "
        f"report={args.output}"
    )
    return 0


__all__ = ["build_scorecard", "load_signals", "main", "proposed_catalog", "render_markdown"]
