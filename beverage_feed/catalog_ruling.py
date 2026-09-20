"""Catalog ruling pack (R2) — decision material for the catalog-size ruling.

Zero egress, read-only. Extends the comparability scorecard with:

- per-retailer evidence strength (candidates / approved Catalog Mappings /
  listing rejections / GTIN hits),
- the ff-16 variant families the retailers actually name in listings but the
  Benchmark Catalog has no (brand, variant) cell for,
- three concrete draft catalogs:
    (a) comparable core (~30 packs, proven first, ranked by evidence),
    (b) stay-at-100,
    (c) variant-expanded (core + concrete new packs from the ff-16 evidence),
  each with its remaining discovery/review cell load (5 retailers minus
  approved mappings minus do_not_map cell rejections).

Feilim makes the ruling; ``data/catalog.json`` is never edited here.
"""

from __future__ import annotations

import argparse
import json
import re
import sqlite3
import sys
from collections import Counter, defaultdict
from contextlib import closing
from pathlib import Path
from typing import Any

from .collector import load_catalog, timestamp
from .feed_reads import open_readonly

RETAILERS: tuple[str, ...] = ("dunnes", "supervalu", "tesco", "lidl", "aldi")

#: Target size for the comparable-core draft (option a).
CORE_TARGET = 30

#: New Catalog Packs option (c) would add, each tied to an ff-16 variant
#: family key below. Derived from the listing names the retailers actually
#: use; the evidence block is filled in from live data at build time.
_PROPOSED_VARIANT_PACKS: list[dict[str, Any]] = [
    {
        "name": "7UP Zero Sugar 330ml Can", "brand": "7UP", "variant": "Zero Sugar",
        "pack_count": 1, "unit_size_ml": 330, "package_type": "can",
        "search_term": "7UP Zero Sugar Can 330ml", "aliases": ["7Up Zero Sugar Can"],
        "variant_family": "7up: zero",
    },
    {
        "name": "7UP Zero Sugar Bottle 500ml", "brand": "7UP", "variant": "Zero Sugar",
        "pack_count": 1, "unit_size_ml": 500, "package_type": "bottle",
        "search_term": "7Up Zero Sugar Bottle 500ml", "aliases": [],
        "variant_family": "7up: zero",
    },
    {
        "name": "7UP Zero Sugar Bottle 1.25 Litres", "brand": "7UP", "variant": "Zero Sugar",
        "pack_count": 1, "unit_size_ml": 1250, "package_type": "bottle",
        "search_term": "7Up Zero Sugar Bottle 1.25 Litres", "aliases": [],
        "variant_family": "7up: zero",
    },
    {
        "name": "7UP Zero Sugar Bottle 2 Litres", "brand": "7UP", "variant": "Zero Sugar",
        "pack_count": 1, "unit_size_ml": 2000, "package_type": "bottle",
        "search_term": "7Up Zero Sugar Bottle 2 Litres", "aliases": ["7Up Zero Sugar 2L"],
        "variant_family": "7up: zero",
    },
    {
        "name": "Dr Pepper Zero 500ml", "brand": "Dr Pepper", "variant": "Zero",
        "pack_count": 1, "unit_size_ml": 500, "package_type": "bottle",
        "search_term": "Dr Pepper Zero 500ml", "aliases": [],
        "variant_family": "dr pepper: zero",
    },
    {
        "name": "Dr Pepper Zero 2 Litres", "brand": "Dr Pepper", "variant": "Zero",
        "pack_count": 1, "unit_size_ml": 2000, "package_type": "bottle",
        "search_term": "Dr Pepper Zero 2L", "aliases": ["Dr Pepper Zero Bottle 2 Litres"],
        "variant_family": "dr pepper: zero",
    },
    {
        "name": "Red Bull Sugarfree 355ml", "brand": "Red Bull", "variant": "Sugarfree",
        "pack_count": 1, "unit_size_ml": 355, "package_type": "can",
        "search_term": "Red Bull Energy Drink, Sugar Free, 355ml", "aliases": [],
        "variant_family": "red bull: sugar free",
    },
    {
        "name": "Lucozade Zero Sugar 380ml", "brand": "Lucozade", "variant": "Zero Sugar",
        "pack_count": 1, "unit_size_ml": 380, "package_type": "bottle",
        "search_term": "Lucozade Energy Zero Sugar Drink Original 380ml", "aliases": [],
        "variant_family": "lucozade: zero",
    },
    {
        "name": "Volvic Touch of Fruit Sugar Free 1.5 Litres", "brand": "Volvic",
        "variant": "Touch of Fruit Sugar Free",
        "pack_count": 1, "unit_size_ml": 1500, "package_type": "bottle",
        "search_term": "Volvic Touch of Fruit Sugar Free 1.5L", "aliases": [],
        "variant_family": "volvic: sugar free",
    },
    {
        "name": "Pepsi Cream Soda Zero Sugar 330ml Can", "brand": "Pepsi",
        "variant": "Cream Soda Zero Sugar",
        "pack_count": 1, "unit_size_ml": 330, "package_type": "can",
        "search_term": "Pepsi Cream Soda Flavour Zero Sugar Can 330ml", "aliases": [],
        "variant_family": "pepsi: zero",
    },
]

#: Variant keywords that mark a listing as a sibling variant, not junk.
_VARIANT_WORDS = re.compile(
    r"(sugar ?free|sugarfree|zero|free|cherry|vanilla|lemonade|pink|max|cocalada|tropical)",
    re.I,
)


def _norm(text: str | None) -> str:
    return re.sub(r"[^a-z0-9 ]", " ", (text or "").lower()).strip()


def _collect_pack_evidence(
    connection: sqlite3.Connection, catalog_ids: set[str]
) -> dict[str, dict[str, Any]]:
    """Per-pack per-retailer evidence: candidates, approved mappings, rejections."""
    evidence: dict[str, dict[str, Any]] = {
        catalog_id: {
            "candidates": defaultdict(int),
            "approved": defaultdict(int),
            "rejections": defaultdict(int),
            "do_not_map": set(),
        }
        for catalog_id in catalog_ids
    }
    for catalog_id, retailer, count in connection.execute(
        "SELECT catalog_id, retailer, COUNT(*) FROM discovery_candidate_cells "
        "WHERE catalog_id IS NOT NULL GROUP BY catalog_id, retailer"
    ):
        if catalog_id in evidence:
            evidence[catalog_id]["candidates"][retailer] = count
    for catalog_id, retailer in connection.execute(
        "SELECT catalog_id, retailer FROM catalog_mappings WHERE status='approved'"
    ):
        if catalog_id in evidence:
            evidence[catalog_id]["approved"][retailer] += 1
    for catalog_id, retailer, count in connection.execute(
        "SELECT catalog_id, retailer, COUNT(*) FROM discovery_rejections "
        "WHERE section='listings' AND state='rejected' AND catalog_id IS NOT NULL "
        "GROUP BY catalog_id, retailer"
    ):
        if catalog_id in evidence:
            evidence[catalog_id]["rejections"][retailer] = count
    for catalog_id, retailer in connection.execute(
        "SELECT catalog_id, retailer FROM discovery_rejections "
        "WHERE section='cells' AND state='do_not_map'"
    ):
        if catalog_id in evidence:
            evidence[catalog_id]["do_not_map"].add(retailer)
    return evidence


def _collect_gtin_hits(connection: sqlite3.Connection) -> Counter:
    """GTIN/EAN hits per retailer, from stored candidate raw records."""
    hits: Counter = Counter()
    for retailer, raw_record in connection.execute(
        "SELECT retailer, raw_record FROM catalog_candidates"
    ):
        try:
            record = json.loads(raw_record) if raw_record else None
        except ValueError:
            record = None
        if isinstance(record, dict) and any(
            record.get(key) for key in ("gtin", "ean", "barcode")
        ):
            hits[retailer] += 1
    return hits


def _collect_variant_evidence(
    connection: sqlite3.Connection, catalog: list[Any]
) -> list[dict[str, Any]]:
    """ff-16 evidence: variants the listings name that no catalog pack carries.

    A listing counts for (brand, stated-variant) when its name mentions the
    brand, states a variant keyword, and states none of the catalog variant
    phrasings already tracked for that brand (pack variants and Brand Alias
    phrasings). Rejected listings are excluded. Per-family example names and
    naming retailers travel with each row.
    """
    by_brand: dict[str, set[str]] = defaultdict(set)
    for pack in catalog:
        phrasings = [pack.variant or "", *pack.aliases]
        by_brand[_norm(pack.brand)].update(p.lower() for p in phrasings if p)
    families: dict[str, dict[str, Any]] = {}
    for retailer, name in connection.execute(
        "SELECT DISTINCT retailer, source_product_name FROM catalog_candidates "
        "WHERE status <> 'rejected' AND source_product_name IS NOT NULL"
    ):
        low = name.lower()
        for brand_key, known in by_brand.items():
            if not brand_key or brand_key not in _norm(low):
                continue
            stated = _VARIANT_WORDS.findall(low)
            if not stated or any(phrase in low for phrase in known):
                continue
            family = f"{brand_key}: {'/'.join(sorted(set(stated)))}"
            entry = families.setdefault(
                family,
                {"family": family, "listings": 0, "retailers": set(), "names": []},
            )
            entry["listings"] += 1
            entry["retailers"].add(retailer)
            if name not in entry["names"] and len(entry["names"]) < 5:
                entry["names"].append(name)
    return [
        {
            "family": entry["family"],
            "listings": entry["listings"],
            "retailers": sorted(entry["retailers"]),
            "example_names": sorted(entry["names"])[:5],
        }
        for entry in sorted(families.values(), key=lambda e: -e["listings"])
    ]


def _pack_row(
    pack: Any, evidence: dict[str, dict[str, Any]]
) -> dict[str, Any]:
    """One ranked row: pack identity plus per-retailer evidence and open cells."""
    entry = evidence[pack.catalog_id]
    approved = sorted(entry["approved"])
    blocked = entry["do_not_map"]
    candidates = sorted(entry["candidates"])
    open_cells: list[str] = [
        retailer for retailer in RETAILERS
        if retailer not in set(approved) | blocked
    ]
    return {
        "catalog_id": pack.catalog_id,
        "name": pack.name,
        "brand": pack.brand,
        "approved_retailers": approved,
        "candidate_retailers": candidates,
        "rejection_count": sum(entry["rejections"].values()),
        "candidate_count": sum(entry["candidates"].values()),
        "evidence_retailers": len(set(approved) | set(candidates)),
        "open_cells": open_cells,
    }


def _select_core(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Option (a): proven packs (>=2 approved mappings) first, then the
    next-ranked comparable packs until CORE_TARGET packs are present."""
    core: list[dict[str, Any]] = [
        row for row in rows if len(row["approved_retailers"]) >= 2
    ]
    for row in rows:
        if len(core) >= CORE_TARGET:
            break
        if row["evidence_retailers"] >= 2 and row not in core:
            core.append(row)
    return core


def _new_packs(
    variants: list[dict[str, Any]]
) -> list[dict[str, Any]]:
    """Concrete option-(c) packs, each annotated with its ff-16 family evidence."""
    by_family = {entry["family"]: entry for entry in variants}
    return [
        {**proposal, "evidence": by_family.get(proposal["variant_family"])}
        for proposal in _PROPOSED_VARIANT_PACKS
    ]


def build_ruling_pack(
    catalog_path: str | Path, database: str | Path
) -> dict[str, Any]:
    """Assemble the whole decision pack from local data (zero egress)."""
    catalog = load_catalog(Path(catalog_path))
    with closing(open_readonly(database)) as connection:
        evidence = _collect_pack_evidence(connection, {p.catalog_id for p in catalog})
        gtin_hits = _collect_gtin_hits(connection)
        variants = _collect_variant_evidence(connection, catalog)
        retailer_summary = {
            retailer: {
                "candidates": connection.execute(
                    "SELECT COUNT(*) FROM catalog_candidates WHERE retailer=?", (retailer,)
                ).fetchone()[0],
                "approved_mappings": connection.execute(
                    "SELECT COUNT(*) FROM catalog_mappings WHERE retailer=? "
                    "AND status='approved'", (retailer,),
                ).fetchone()[0],
                "listing_rejections": connection.execute(
                    "SELECT COUNT(*) FROM discovery_rejections WHERE retailer=? "
                    "AND section='listings' AND state='rejected'", (retailer,),
                ).fetchone()[0],
                "gtin_hits": gtin_hits.get(retailer, 0),
            }
            for retailer in RETAILERS
        }

    rows = [_pack_row(pack, evidence) for pack in catalog]
    rows.sort(key=lambda row: (
        -row["evidence_retailers"],
        -len(row["approved_retailers"]),
        -row["candidate_count"],
        row["name"],
    ))
    comparable = [row for row in rows if row["evidence_retailers"] >= 2]
    proven = [row for row in rows if len(row["approved_retailers"]) >= 2]

    core = _select_core(rows)
    new_packs = _new_packs(variants)
    drafts = {
        "a_comparable_core": {
            "catalog_ids": sorted(row["catalog_id"] for row in core),
            "packs": len(core),
            "cells_left": sum(len(row["open_cells"]) for row in core),
        },
        "b_stay_at_100": {
            "catalog_ids": sorted(row["catalog_id"] for row in rows),
            "packs": len(rows),
            "cells_left": sum(len(row["open_cells"]) for row in rows),
        },
        "c_variant_expanded": {
            "catalog_ids": sorted(row["catalog_id"] for row in core),
            "new_packs": new_packs,
            "packs": len(core) + len(new_packs),
            "cells_left": sum(len(row["open_cells"]) for row in core)
            + len(new_packs) * len(RETAILERS),
        },
    }
    return {
        "generated_at": timestamp(),
        "retailer_summary": retailer_summary,
        "headline": {
            "packs_scored": len(rows),
            "comparable": len(comparable),
            "proven": len(proven),
        },
        "drafts": drafts,
        "rows": rows,
        "variant_evidence": variants,
    }


def render_markdown(pack: dict[str, Any]) -> str:
    """Render the human-readable decision pack."""
    drafts = pack["drafts"]
    a, b, c = drafts["a_comparable_core"], drafts["b_stay_at_100"], drafts["c_variant_expanded"]
    headline = pack["headline"]
    lines = [
        "# Catalog ruling pack (R2)",
        "",
        f"Generated {pack['generated_at']} by `beverage_feed/catalog_ruling.py` — "
        "read-only join of `data/catalog.json` × the discovery database. "
        "Zero retailer requests. **Feilim rules; no `data/catalog.json` edit "
        "happened** — the JSON decision files are identity of record until a "
        "human edits the catalog.",
        "",
        "## Headline",
        "",
        f"- Packs scored: **{headline['packs_scored']}**",
        f"- Comparable (≥2 retailers with evidence): **{headline['comparable']}**",
        f"- Proven (≥2 approved Catalog Mappings): **{headline['proven']}**",
        "",
        "## Draft catalogs (remaining cells = 5 retailers − approved mappings − do_not_map rejections)",
        "",
        "| option | what | packs | cells left |",
        "|---|---|---|---|",
        f"| a | comparable core (proven first, then ranked comparable) "
        f"| {a['packs']} | {a['cells_left']} |",
        f"| b | stay-at-100 (unchanged) | {b['packs']} | {b['cells_left']} |",
        f"| c | core + {len(c['new_packs'])} new variant packs "
        f"(added cell cost: {len(c['new_packs'])} × 5 = "
        f"{len(c['new_packs']) * len(RETAILERS)}) | {c['packs']} | {c['cells_left']} |",
        "",
        "vs the current 100: (a) drops "
        f"{b['packs'] - a['packs']} cells-laden single-retailer/no-signal packs "
        f"and cuts the cell load by {b['cells_left'] - a['cells_left']}; "
        f"(c) keeps that core and re-adds {len(c['new_packs'])} "
        "evidence-backed variant packs for "
        f"{len(c['new_packs']) * len(RETAILERS)} extra cells.",
        "",
        "### Option (c) new Catalog Packs (from ff-16 evidence)",
        "",
        "| new pack | ff-16 family | family listings | retailers |",
        "|---|---|---|---|",
    ]
    for new_pack in c["new_packs"]:
        evidence = new_pack.get("evidence") or {}
        lines.append(
            f"| {new_pack['name']} | {new_pack['variant_family']} "
            f"| {evidence.get('listings', 0)} "
            f"| {', '.join(evidence.get('retailers', [])) or '-'} |"
        )
    lines += [
        "",
        "## Per-retailer evidence strength",
        "",
        "| retailer | candidates | approved mappings | listing rejections | GTIN hits |",
        "|---|---|---|---|---|",
    ]
    for retailer, entry in pack["retailer_summary"].items():
        lines.append(
            f"| {retailer} | {entry['candidates']} | {entry['approved_mappings']} "
            f"| {entry['listing_rejections']} | {entry['gtin_hits']} |"
        )
    lines += [
        "",
        "## ff-16 variant evidence (named by listings, no catalog cell)",
        "",
        "A listing counts for a family when its name mentions a catalog brand, "
        "states a variant keyword, and states none of the variant/alias "
        "phrasings that brand already has in the catalog. Rejected listings "
        "are excluded. (Known strict (brand,variant)-pair gap: Jack Daniel's & "
        "Coca-Cola Zero Sugar — 3 SuperValu listings, already rejected, so it "
        "does not appear below.) Junk siblings (e.g. Berocca) are review-time "
        "rejects under ff-15, not catalog material.",
        "",
        "| family | listings | retailers | example listing names |",
        "|---|---|---|---|",
    ]
    for entry in pack["variant_evidence"]:
        names = "<br>".join(f"`{name}`" for name in entry["example_names"])
        lines.append(
            f"| {entry['family']} | {entry['listings']} "
            f"| {', '.join(entry['retailers']) or '-'} | {names} |"
        )
    lines += [
        "",
        "### Option (a) core packs",
        "",
    ]
    for catalog_id in a["catalog_ids"]:
        lines.append(f"- `{catalog_id}`")
    lines.append("")
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="R2: catalog ruling pack (read-only, zero egress)"
    )
    parser.add_argument("--catalog", type=Path, default=Path("data/catalog.json"))
    parser.add_argument("--database", type=Path, default=Path("data/feed.sqlite"))
    parser.add_argument("--output", type=Path, default=Path("research/catalog-ruling-pack.md"))
    parser.add_argument("--json", type=Path, default=Path("research/catalog-proposal.json"))
    args = parser.parse_args(argv)
    try:
        pack = build_ruling_pack(args.catalog, args.database)
    except (sqlite3.Error, FileNotFoundError) as exc:
        print(f"ruling-pack: {exc}")
        return 1
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.json.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(render_markdown(pack))
    args.json.write_text(json.dumps(pack, indent=2, sort_keys=True) + "\n")
    headline = pack["headline"]
    print(
        f"ruling-pack: packs={headline['packs_scored']} "
        f"comparable={headline['comparable']} proven={headline['proven']} "
        f"markdown={args.output} json={args.json}"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
