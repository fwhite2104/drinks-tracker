"""Mechanical junk block proposals (ff-15 R1b) — zero egress, read-only.

Groups the active cell-scoped listing rejections in ``data/rejections.json``
by (retailer, candidate) and proposes a **retailer block** when:

- the candidate was rejected in ``--min-cells`` (default 2) or more cells, and
- every rejection reason reads as not-a-beverage / wrong product
  ("sweets", "lolly", "keyword noise", "wrong brand", "different brand /
  product", "beer", "not a beverage") — never a variant dispute
  ("wrong variant/size/flavour/line", multipack, pack-count phrasing).

Variant-ambiguous groups go to the Feilim batch, never auto-applied.

Two modes:

- default: print the report (JSON on stdout) and write the markdown batch
  summary to the path given by ``--report``;
- ``--apply``: additionally apply every unambiguous proposal through
  ``discovery_cli.block_listing_everywhere`` (JSON-first, then the store)
  with ``decided_by="agent-block-pass"`` per the standing agent-sprint
  rules; Feilim spot-checks afterwards.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any

from .collector import timestamp
from .discovery import load_rejections
from .discovery_cli import block_listing_everywhere
from .discovery import DiscoveryStore

#: Junk evidence: not a beverage / a different product entirely.
_JUNK_REASON = re.compile(
    r"sweet|lolly|keyword noise|wrong brand|not .*drink|not a beverage"
    r"|not a real product|battery|different product category"
    r"|different brand/product|\bbeer\b",
    re.I,
)
#: Variant disputes stay per-cell rejections (or become ff-16 catalog cells).
_VARIANT_REASON = re.compile(
    r"variant|wrong size|multipack|pack count|wrong pack|wrong format"
    r"|wrong flavour|wrong line",
    re.I,
)

DECIDED_BY = "agent-block-pass"


def propose_blocks(
    rejections_path: str | Path,
    *,
    min_cells: int = 2,
) -> dict[str, Any]:
    """Classify multi-cell rejected candidates into proposal batches.

    Returns a JSON-serialisable report: per-retailer junk proposals (with
    evidence) and the variant-ambiguous remainder for Feilim's batch.
    """
    rejections = load_rejections(rejections_path)
    groups: dict[tuple[str, str], list[dict[str, Any]]] = defaultdict(list)
    for row in rejections["listings"]:
        if row.get("state") == "rejected":
            groups[(row.get("retailer", ""), row.get("canonical_key", ""))].append(row)

    junk: list[dict[str, Any]] = []
    variant: list[dict[str, Any]] = []
    for (retailer, candidate_id), rows in sorted(groups.items()):
        if len(rows) < min_cells:
            continue
        reasons = sorted({row.get("reason", "") for row in rows})
        joined = " || ".join(reasons)
        cells = sorted({row["cell"] for row in rows if row.get("cell") is not None})
        if _VARIANT_REASON.search(joined):
            variant.append({
                "retailer": retailer,
                "candidate_id": candidate_id,
                "rejected_cells": len(rows),
                "reasons": reasons,
            })
        elif _JUNK_REASON.search(joined):
            junk.append({
                "retailer": retailer,
                "candidate_id": candidate_id,
                "rejected_cells": len(rows),
                "reasons": reasons,
                "cells": cells,
            })

    by_retailer: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for proposal in junk:
        by_retailer[proposal["retailer"]].append(proposal)
    return {
        "generated_at": timestamp(),
        "junk_proposals": junk,
        "variant_ambiguous": variant,
        "by_retailer": dict(by_retailer),
        "counts": {
            "junk_proposals": len(junk),
            "variant_ambiguous": len(variant),
            "rows_covered": sum(item["rejected_cells"] for item in junk),
        },
    }


def apply_blocks(
    report: dict[str, Any],
    *,
    database: str | Path,
    rejection_path: str | Path,
    decided_by: str = DECIDED_BY,
) -> dict[str, Any]:
    """Apply the unambiguous junk proposals through the real CLI seam."""
    store = DiscoveryStore(database)
    applied: list[dict[str, Any]] = []
    failed: list[dict[str, Any]] = []
    for proposal in report["junk_proposals"]:
        try:
            result = block_listing_everywhere(
                store,
                retailer=proposal["retailer"],
                candidate_id=proposal["candidate_id"],
                rejection_path=rejection_path,
                decided_by=decided_by,
                reason=proposal["reasons"][0],
            )
            applied.append({
                "retailer": proposal["retailer"],
                "candidate_id": proposal["candidate_id"],
                "idempotent": bool(result.get("idempotent")),
                "cells_cleared": int(result.get("cells_cleared") or 0),
            })
        except ValueError as exc:
            failed.append({
                "retailer": proposal["retailer"],
                "candidate_id": proposal["candidate_id"],
                "error": str(exc),
            })
    return {
        "applied_count": len(applied),
        "skipped_count": len(failed),
        "cells_cleared_total": sum(item["cells_cleared"] for item in applied),
        "applied": applied,
        "failed": failed,
    }


def render_markdown(report: dict[str, Any], *, generated_at: str) -> str:
    """Batch summary for Feilim's spot-check."""
    counts = report["counts"]
    lines = [
        f"# Retailer block proposals — {generated_at}",
        "",
        "ff-15 R1b: mechanical pass over `data/rejections.json` (zero egress).",
        "A block is proposed when one candidate was cell-rejected in ≥2 cells and",
        "every reason is junk (not a beverage / wrong product) — never a variant",
        "dispute. Variant-ambiguous groups stay in Feilim's batch.",
        "",
        f"- unambiguous junk proposals: **{counts['junk_proposals']}**",
        f"- variant-ambiguous (Feilim batch): **{counts['variant_ambiguous']}**",
        f"- cell-scoped rows covered by the proposals: **{counts['rows_covered']}**",
        "",
        "## Junk proposals (auto-applicable via `agent-block-pass`)",
        "",
        "| retailer | candidate | cells | reason |",
        "|---|---|---|---|",
    ]
    for proposal in report["junk_proposals"]:
        reason = (proposal["reasons"][0] or "")[:90].replace("|", "\\|")
        lines.append(
            f"| {proposal['retailer']} | `{proposal['candidate_id']}` "
            f"| {proposal['rejected_cells']} | {reason} |"
        )
    lines += [
        "",
        "## Variant-ambiguous (Feilim batch, never auto-applied)",
        "",
        "| retailer | candidate | cells | reasons |",
        "|---|---|---|---|",
    ]
    for proposal in report["variant_ambiguous"]:
        reasons = " \\| ".join(reason[:60] for reason in proposal["reasons"][:2])
        lines.append(
            f"| {proposal['retailer']} | `{proposal['candidate_id']}` "
            f"| {proposal['rejected_cells']} | {reasons} |"
        )
    lines.append("")
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=(
            "ff-15 R1b: propose retailer blocks for multi-cell junk candidates "
            "(read-only; with --apply, through the real decision seam)"
        )
    )
    parser.add_argument("--rejections", type=Path, default=Path("data/rejections.json"))
    parser.add_argument("--database", type=Path, default=Path("data/feed.sqlite"))
    parser.add_argument("--report", type=Path, default=Path("research/block-proposals.md"))
    parser.add_argument("--min-cells", type=int, default=2)
    parser.add_argument(
        "--apply", action="store_true",
        help="apply unambiguous junk proposals with decided_by=agent-block-pass",
    )
    args = parser.parse_args(argv)

    report = propose_blocks(args.rejections, min_cells=args.min_cells)
    generated_at = report["generated_at"]
    if args.apply:
        outcome = apply_blocks(
            report, rejection_path=args.rejections, database=args.database,
        )
        report["apply_outcome"] = outcome
    lines = render_markdown(report, generated_at=generated_at)
    if args.apply:
        outcome = report["apply_outcome"]
        lines = lines.replace(
            f"# Retailer block proposals — {generated_at}",
            f"# Retailer block proposals — {generated_at}\n\n"
            f"**Applied this run:** {outcome['applied_count']} blocks "
            f"({outcome['cells_cleared_total']} cells cleared), "
            f"{outcome['skipped_count']} skipped "
            f"(decided_by={DECIDED_BY}; Feilim spot-checks).",
        )
    args.report.parent.mkdir(parents=True, exist_ok=True)
    args.report.write_text(lines)
    counts = report["counts"]
    print(
        f"block-proposals: junk={counts['junk_proposals']} "
        f"variant_ambiguous={counts['variant_ambiguous']} "
        f"report={args.report}"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
