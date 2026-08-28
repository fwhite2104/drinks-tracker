"""Evidence classification pass over persisted discovery evidence.

Re-runs the matcher over every stored evidence row and classifies each
candidate-cell into the trial classes decided in ticket 04:

- **A clean**: unique attribute candidate, zero diffs, every pack-defining
  attribute inferred from the listing name, price valid → batch-approve with
  a 10% spot-check; any surprise demotes the whole batch to per-item.
- **B name disagreement**: per-item eyeball.
- **C ambiguous**: multiple catalog packs share the attributes, or diffs are
  non-empty → per-item.
- **D price missing**: otherwise clean → defer to the term-expansion re-run.

The universal junk relevance gate (CONTEXT.md: Catalog Mapping Discovery) is
applied during classification, so POWERCUT/LED-lamp-class rows are set aside
(``excluded``) rather than classified.  Output: class counts, the
sprint-ready batch lists the dashboard can serve, and the list of thin and
Class-D cells that feeds the re-run's targeting (ticket 14).

Discovery records evidence and mapping decisions only; this pass never
creates a Price Observation.  Where the candidate's raw record survives, the
matcher re-runs over a fresh extraction (so the Brand Alias layer applies to
legacy evidence); otherwise the stored normalized evidence is used as-is.
"""

from __future__ import annotations

import json
import sqlite3
from contextlib import closing
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable, Mapping

from .collector import BenchmarkPack, timestamp
from .discovery import DiscoveryStore
from .discovery_adapters import normalize_listing
from .matching import (
    SourceListing,
    attribute_candidates,
    is_relevant_candidate,
    name_matches,
)

EVIDENCE_CLASSES = ("A", "B", "C", "D")
EXCLUDED = "excluded"

# Attributes the exact-pack bar judges (CONTEXT.md: Catalog Mapping).
PACK_DEFINING_ATTRIBUTES = ("brand", "variant", "pack_count", "unit_size_ml", "package_type")

# Inference bases that mean "the listing name itself carries this attribute"
# (brand-alias is a curated bridge applied to the name's own phrase).
_NAME_DERIVED_BASIS = {"name", "brand-alias"}

# Cell states that never need re-run targeting: the mapping decision is made.
_DECIDED_CELL_STATES = {"approved", "rejected", "do_not_map"}


@dataclass(frozen=True)
class EvidenceFacts:
    """One candidate-cell's matcher inputs, re-extracted or read from store."""

    name: str
    attributes: Mapping[str, Any]
    inference_basis: Mapping[str, str]
    attribute_diffs: Mapping[str, Any]
    price_status: str


def _name_derived(basis: str | None) -> bool:
    """True when the attribute's inference basis is the listing name itself."""
    return basis is not None and (
        basis in _NAME_DERIVED_BASIS or basis.startswith("derived")
    )


def classify_candidate_cell(
    pack: BenchmarkPack,
    facts: EvidenceFacts,
    catalog: Iterable[BenchmarkPack],
) -> tuple[str, list[str]]:
    """Classify one candidate-cell's evidence against the catalog.

    Returns the class (A-D, or ``excluded`` when the junk gate sets the row
    aside) and the reasons recorded for the review sprint.
    """
    if not is_relevant_candidate(facts.name, pack):
        return EXCLUDED, ["junk gate: listing name shares no identity token with the pack"]
    listing = SourceListing(
        retailer=pack.catalog_id,
        source_product_reference="",
        source_item_id="",
        name=facts.name,
        brand=facts.attributes.get("brand"),
        variant=facts.attributes.get("variant"),
        pack_count=facts.attributes.get("pack_count"),
        unit_size_ml=facts.attributes.get("unit_size_ml"),
        package_type=facts.attributes.get("package_type"),
    )
    candidates = attribute_candidates(catalog, listing)
    if len(candidates) > 1:
        return "C", ["multiple catalog packs share the listing attributes"]
    if facts.attribute_diffs:
        keys = ",".join(sorted(facts.attribute_diffs))
        return "C", [f"attribute conflicts between structured data and the listing name: {keys}"]
    if not candidates:
        return "B", ["listing attributes match no catalog pack"]
    if candidates[0].catalog_id != pack.catalog_id:
        return "B", [
            "listing attributes uniquely match another catalog pack: "
            f"{candidates[0].catalog_id}"
        ]
    if not name_matches(pack, listing):
        return "B", ["listing name does not contain the pack name or aliases"]
    structured = sorted(
        key for key in PACK_DEFINING_ATTRIBUTES
        if facts.attributes.get(key) is not None
        and not _name_derived(facts.inference_basis.get(key))
    )
    if structured:
        return "B", [
            f"attributes not inferred from the listing name: {', '.join(structured)}"
        ]
    if facts.price_status != "valid":
        return "D", [f"price is not usable: price_parse_status={facts.price_status}"]
    return "A", []


def _cell_class(cell_entries: list[dict[str, Any]]) -> str:
    """Roll a cell's candidate-cell classifications up to one sprint class.

    - **A batch-ready**: at least one clean candidate and no ambiguous one;
      price-less or other-pack siblings do not block the clean candidate's
      approval, and several clean candidates naming the same listing are
      duplicate identities of one product.
    - **C per-item**: any ambiguous candidate, or clean candidates whose
      names disagree (which product is the mapping? — the existing
      conflicting-candidates case).
    - **B per-item**: name disagreement only.
    - **D re-run**: price missing only.
    """
    classes = [entry["class"] for entry in cell_entries]
    if "C" in classes:
        return "C"
    clean_names = {entry["name"] for entry in cell_entries if entry["class"] == "A"}
    if len(clean_names) > 1:
        return "C"
    if clean_names:
        return "A"
    if "B" in classes:
        return "B"
    return "D"


def _spot_check(batch: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Deterministic ~10% sample of a Class-A batch, evenly spaced."""
    if not batch:
        return []
    size = max(1, len(batch) // 10)
    return [batch[index * len(batch) // size] for index in range(size)]


def _record(raw_record: Any) -> Mapping[str, Any] | None:
    """Parse a stored candidate raw record, or None when unusable."""
    if not isinstance(raw_record, str) or not raw_record.strip():
        return None
    try:
        parsed = json.loads(raw_record)
    except ValueError:
        return None
    # upsert_candidate stores "{}" when a candidate has no raw record; an
    # empty mapping is unusable, so the stored-evidence fallback applies.
    return parsed if isinstance(parsed, Mapping) and parsed else None


def _mapping(raw: Any) -> Mapping[str, Any]:
    if isinstance(raw, Mapping):
        return raw
    if isinstance(raw, str) and raw.strip():
        try:
            parsed = json.loads(raw)
        except ValueError:
            return {}
        if isinstance(parsed, Mapping):
            return parsed
    return {}


def _evidence_facts(
    retailer: str,
    row: sqlite3.Row,
    candidate: sqlite3.Row | None,
) -> EvidenceFacts:
    """Re-extract the matcher inputs, falling back to the stored evidence."""
    record = _record(candidate["raw_record"] if candidate is not None else None)
    if record is not None:
        listing = normalize_listing(retailer, record)
        return EvidenceFacts(
            name=listing.name,
            attributes=dict(listing.attributes),
            inference_basis=dict(listing.inference_basis),
            attribute_diffs=dict(listing.conflicts),
            price_status=listing.price.status,
        )
    return EvidenceFacts(
        name=(candidate["source_product_name"] if candidate is not None else "") or "",
        attributes=_mapping(row["normalized_attributes"]),
        inference_basis={
            key: value for key, value in _mapping(row["inference_basis"]).items()
            if isinstance(value, str)
        },
        attribute_diffs=_mapping(row["attribute_diffs"]),
        price_status=row["price_parse_status"] or "missing",
    )


def classify_evidence(
    catalog: list[BenchmarkPack],
    store: DiscoveryStore,
    *,
    retailer: str | None = None,
) -> dict[str, Any]:
    """Classify every candidate-cell's latest evidence into classes A-D.

    The classification unit is the candidate-cell (candidate x retailer x
    catalog cell); only its latest evidence row counts.  Class counts are
    reported at both candidate-cell and cell granularity, batches list the
    sprint-ready candidate-cells per class, and the re-run targeting list
    combines Class-D cells with thin cells (non-decided cells with no
    classifiable evidence).
    """
    packs = {pack.catalog_id: pack for pack in catalog}
    where = " WHERE retailer=?" if retailer is not None else ""
    parameters: tuple[str, ...] = () if retailer is None else (retailer,)
    with closing(store.connection()) as connection:
        connection.row_factory = sqlite3.Row
        candidates = {
            row["candidate_id"]: row
            for row in connection.execute(
                f"SELECT candidate_id, retailer, source_product_name, raw_record "
                f"FROM catalog_candidates{where}",
                parameters,
            )
        }
        # Latest evidence row per candidate-cell wins: later runs re-extract.
        latest: dict[tuple[str, str, str], sqlite3.Row] = {}
        evidence_rows = 0
        for row in connection.execute(
            f"SELECT * FROM discovery_candidate_evidence{where} "
            f"ORDER BY evidence_id",
            parameters,
        ):
            evidence_rows += 1
            latest[(row["candidate_id"], row["retailer"], row["catalog_id"])] = row
        cell_states = {
            (row["retailer"], row["catalog_id"]): row["state"]
            for row in connection.execute("SELECT retailer, catalog_id, state FROM discovery_cells")
        }

    entries: dict[str, list[dict[str, Any]]] = {name: [] for name in EVIDENCE_CLASSES}
    excluded: list[dict[str, Any]] = []
    skipped = 0
    cell_entries: dict[tuple[str, str], list[dict[str, Any]]] = {}
    for key in sorted(latest):
        candidate_id, evidence_retailer, catalog_id = key
        pack = packs.get(catalog_id)
        if pack is None:
            skipped += 1
            continue
        row = latest[key]
        facts = _evidence_facts(evidence_retailer, row, candidates.get(candidate_id))
        classification, reasons = classify_candidate_cell(pack, facts, catalog)
        entry = {
            "retailer": evidence_retailer,
            "catalog_id": catalog_id,
            "candidate_id": candidate_id,
            "name": facts.name,
            "price": row["raw_price_value"],
            "class": classification,
            "reasons": reasons,
            "cell_state": cell_states.get((evidence_retailer, catalog_id)),
        }
        if classification == EXCLUDED:
            excluded.append(entry)
            continue
        entries[classification].append(entry)
        cell_entries.setdefault((evidence_retailer, catalog_id), []).append(entry)

    for batch in entries.values():
        batch.sort(key=lambda entry: (entry["retailer"], entry["catalog_id"], entry["candidate_id"]))
    excluded.sort(key=lambda entry: (entry["retailer"], entry["catalog_id"], entry["candidate_id"]))
    for batch in entries.values():
        for entry in batch:
            cell_key = (entry["retailer"], entry["catalog_id"])
            entry["cell_class"] = _cell_class(cell_entries[cell_key])

    cell_counts = {name: 0 for name in EVIDENCE_CLASSES}
    unclassified_cells = 0
    rerun_targets: list[dict[str, Any]] = []
    cell_keys: set[tuple[str, str]] = set(cell_entries) | set(cell_states)
    for cell in sorted(cell_keys):
        state = cell_states.get(cell)
        cell_class = _cell_class(cell_entries[cell]) if cell in cell_entries else None
        if cell_class is not None:
            cell_counts[cell_class] += 1
        else:
            unclassified_cells += 1
        decided = state in _DECIDED_CELL_STATES
        if cell_class is None and not decided:
            rerun_targets.append({
                "retailer": cell[0], "catalog_id": cell[1], "state": state,
                "reason": "thin: no classifiable evidence",
            })
        elif cell_class == "D" and not decided:
            rerun_targets.append({
                "retailer": cell[0], "catalog_id": cell[1], "state": state,
                "reason": "class D: price missing",
            })

    classified_total = sum(len(batch) for batch in entries.values())
    return {
        "generated_at": timestamp(),
        "counts": {
            "evidence_rows": evidence_rows,
            "candidate_cells": {
                "A": len(entries["A"]),
                "B": len(entries["B"]),
                "C": len(entries["C"]),
                "D": len(entries["D"]),
                "excluded": len(excluded),
                "skipped": skipped,
                "total": classified_total + len(excluded),
            },
            "cells": {
                "A": cell_counts["A"],
                "B": cell_counts["B"],
                "C": cell_counts["C"],
                "D": cell_counts["D"],
                "unclassified": unclassified_cells,
                "total": len(cell_entries) + unclassified_cells,
            },
        },
        "batches": entries,
        "spot_check": _spot_check(entries["A"]),
        "excluded": excluded,
        "rerun_targets": rerun_targets,
        "thin_cells": [
            target for target in rerun_targets if target["reason"].startswith("thin")
        ],
    }


def format_classification(report: dict[str, Any]) -> str:
    """Compact one-block summary of a classification report."""
    candidate_cells = report["counts"]["candidate_cells"]
    cells = report["counts"]["cells"]
    return "\n".join([
        f"evidence classification generated={report['generated_at']}",
        f"candidate_cells total={candidate_cells['total']} A={candidate_cells['A']} "
        f"B={candidate_cells['B']} C={candidate_cells['C']} D={candidate_cells['D']} "
        f"excluded={candidate_cells['excluded']} skipped={candidate_cells['skipped']}",
        f"cells total={cells['total']} A={cells['A']} B={cells['B']} C={cells['C']} "
        f"D={cells['D']} unclassified={cells['unclassified']}",
        f"spot_check={len(report['spot_check'])} "
        f"rerun_targets={len(report['rerun_targets'])} "
        f"(thin={len(report['thin_cells'])})",
    ])


__all__ = [
    "EVIDENCE_CLASSES", "EXCLUDED", "EvidenceFacts", "PACK_DEFINING_ATTRIBUTES",
    "classify_candidate_cell", "classify_evidence", "format_classification",
]
