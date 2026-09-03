"""Deterministic auto-approval and review decisions for discovery candidates.

Auto-approval requires one unique, fully evidenced, collectable exact-pack
candidate.  Everything else routes to an explainable review record.  Durable
JSON commits before SQLite, so reconciliation repairs interrupted decisions.
"""

from __future__ import annotations

import sqlite3
from contextlib import closing
from pathlib import Path
from typing import Any, Iterable, Mapping

from .collector import BenchmarkPack, timestamp
from .discovery import (
    DiscoveryStore, approved_mapping, candidate_id_for, load_mappings, source_fields,
    write_mappings,
)
from .discovery_adapters import DiscoveryAdapter, NormalizedListing
from .matching import brand_matches_alias, name_matches, same_text

_EXACT_ATTRIBUTES = ("brand", "variant", "pack_count", "unit_size_ml", "package_type")
# Fixture case: name_pack_signature is a weak fallback identity and never
# satisfies the stable-identity auto-approval requirement.
_STABLE_TIERS = {"composite", "item", "product", "tpnb"}


def exact_match(pack: BenchmarkPack, listing: Any) -> bool:
    """All five exact-pack attributes must be known and equal."""
    attrs = listing.attributes
    if any(attrs.get(key) is None for key in _EXACT_ATTRIBUTES):
        return False
    return (
        (
            same_text(pack.brand, str(attrs["brand"]))
            or brand_matches_alias(pack, str(attrs["brand"]))
        )
        and same_text(pack.variant, str(attrs["variant"]))
        and attrs["pack_count"] == pack.pack_count
        and attrs["unit_size_ml"] == pack.unit_size_ml
        and attrs["package_type"] == pack.package_type
    )


def _signature(listing: NormalizedListing) -> tuple[Any, ...]:
    # Full normalized attribute set: exact candidates agree on the five
    # pack-defining attributes but may still diverge on derived or
    # structured extras such as total_volume_ml.
    return tuple(sorted(listing.attributes.items()))


def decide_cell(
    store: DiscoveryStore,
    *,
    retailer: str,
    pack: BenchmarkPack,
    candidates: Iterable[NormalizedListing],
    adapter: DiscoveryAdapter,
    mapping_path: str | Path | None,
    run_id: str,
    decided_by: str = "discovery",
    now: str | None = None,
) -> dict[str, Any]:
    """Resolve one cell's candidates into a durable decision.

    ``candidates`` is every listing the cell's searches surfaced; the cell's
    name-matched candidates are the ambiguity set, and auto-approval requires
    exactly one exact, collectable candidate inside it.  Returns
    ``{"decision": "approved" | "review" | "challenge", "category",
    "candidate_id", "reason"}``.  Approved mappings are committed to JSON
    before SQLite cell state; an interrupted pair is repaired by
    reconciliation on the next invocation.
    """
    now = now or timestamp()
    candidates = list(candidates)
    named = [listing for listing in candidates if name_matches(pack, listing)]
    exact = [listing for listing in named if exact_match(pack, listing)]
    mappings = (
        load_mappings(mapping_path)
        if mapping_path is not None and Path(mapping_path).exists()
        else {}
    )
    existing = approved_mapping(mappings, retailer, pack.catalog_id)

    def review(candidate_id: str | None, category: str | None, reason: str, diffs: Mapping[str, Any] | None = None) -> dict[str, Any]:
        if diffs:
            store.diagnostic(
                event="review_attribute_diffs", level="warning", run_id=run_id,
                retailer=retailer, catalog_id=pack.catalog_id, details=diffs,
            )
        store.set_cell_state(
            retailer, pack.catalog_id, "review",
            review_category=category, candidate_id=candidate_id,
            decided_by=decided_by, reason=reason,
        )
        return {"decision": "review", "category": category, "candidate_id": candidate_id, "reason": reason}

    if not exact:
        return review(None, "missing", "no exact-pack candidate among discovery evidence")

    if existing is not None:
        matched = existing.get("matched_source_identity")
        if matched in {listing.source_identity for listing in exact}:
            return {
                "decision": "approved", "category": None,
                "candidate_id": existing.get("candidate_id"),
                "reason": "existing approved mapping re-confirmed",
            }
        challenger = sorted(
            candidate_id_for(retailer, listing.source_identity)
            for listing in exact
            if listing.source_identity != matched
        )[0]
        if existing.get("identity_tier") and not adapter.capabilities.supports(existing["identity_tier"]):
            # Fixture case: a capability downgrade never invalidates the mapping.
            store.diagnostic(
                event="capability_downgrade", level="warning", run_id=run_id,
                message="approved mapping identity tier is no longer supported; mapping retained",
                retailer=retailer, catalog_id=pack.catalog_id,
            )
        store.set_cell_state(
            retailer, pack.catalog_id, "review",
            review_category="challenge", candidate_id=challenger,
            decided_by=decided_by,
            reason="late exact-pack candidate challenges the existing mapping",
        )
        return {
            "decision": "challenge", "category": "challenge",
            "candidate_id": challenger,
            "reason": "late exact-pack candidate challenges the existing mapping",
        }

    collectable = [
        listing for listing in named
        if listing.identity_tier in _STABLE_TIERS
        and adapter.capabilities.supports(listing.identity_tier)
    ]
    exact_collectable = [listing for listing in collectable if listing in exact]
    if len(exact_collectable) == 1 and len(collectable) == 1:
        listing = exact_collectable[0]
        candidate_id = candidate_id_for(retailer, listing.source_identity)
        if mapping_path is None:
            return review(candidate_id, None, "auto-approval requires a durable mapping file")
        reason = "unique collectable exact-pack candidate"
        row = {
            "catalog_id": pack.catalog_id,
            "expected_product_name": pack.name,
            "status": "approved",
            "decision_kind": "auto",
            "decided_by": decided_by,
            "decided_at": now,
            "discovery_run_id": run_id,
            "matched_source_identity": listing.source_identity,
            "identity_tier": listing.identity_tier,
            "candidate_id": candidate_id,
            "decision_reason": reason,
            "auto_approved": True,
            **source_fields(retailer, listing.source_identity, listing.identity_tier),
        }
        mappings.setdefault(retailer, [])
        mappings[retailer].append(row)
        write_mappings(mapping_path, mappings)  # durable JSON first, SQLite second
        store.set_cell_state(
            retailer, pack.catalog_id, "approved",
            candidate_id=candidate_id, decided_by=decided_by, reason=reason,
        )
        store.diagnostic(
            event="auto_approved", run_id=run_id,
            retailer=retailer, catalog_id=pack.catalog_id,
            details={"candidate_id": candidate_id, "identity_tier": listing.identity_tier},
        )
        return {"decision": "approved", "category": None, "candidate_id": candidate_id, "reason": reason}

    if not exact_collectable:
        candidate_id = candidate_id_for(retailer, exact[0].source_identity)
        return review(
            candidate_id, "missing",
            "exact-pack candidate lacks a tested collection/hydration path",
        )

    diffs = {
        candidate_id_for(retailer, listing.source_identity): dict(sorted(listing.attributes.items()))
        for listing in collectable
    }
    reason = (
        "exact duplicates differing only by source identity"
        if len({_signature(listing) for listing in collectable}) == 1
        else "divergent normalized attributes across exact-pack candidates"
    )
    return review(sorted(diffs)[0], "conflicting-candidates", reason, diffs=diffs)


def apply_mapping_replacement(
    store: DiscoveryStore,
    *,
    retailer: str,
    catalog_id: str,
    candidate_id: str,
    mapping_path: str | Path,
    decided_by: str,
    reason: str,
    now: str | None = None,
) -> dict[str, Any]:
    """Atomically remap a cell: old mapping retained, marked superseded_by."""
    now = now or timestamp()
    mappings = load_mappings(mapping_path)
    rows = mappings.get(retailer, [])
    existing = approved_mapping(mappings, retailer, catalog_id)
    if existing is None:
        raise ValueError("no approved mapping to replace")
    candidate = store.validate_candidate_for_cell(
        candidate_id, retailer, catalog_id, require_evidence=True,
    )
    if existing.get("candidate_id") == candidate_id:
        return {"status": "approved", "idempotent": True, "old": existing, "new": existing}

    existing["status"] = "rejected"
    existing["superseded_by"] = candidate_id
    new_row = {
        "catalog_id": catalog_id,
        "expected_product_name": candidate["source_product_name"],
        "status": "approved",
        "decision_kind": "operator",
        "decided_by": decided_by,
        "decided_at": now,
        "matched_source_identity": candidate["identity_key"],
        "identity_tier": candidate["identity_tier"],
        "candidate_id": candidate_id,
        "decision_reason": reason,
        **source_fields(retailer, candidate["identity_key"], candidate["identity_tier"]),
    }
    rows.append(new_row)
    write_mappings(mapping_path, mappings)  # one logical JSON commit
    store.set_cell_state(
        retailer, catalog_id, "approved",
        candidate_id=candidate_id, decided_by=decided_by, reason=reason,
    )
    store.diagnostic(
        event="mapping_replaced",
        retailer=retailer, catalog_id=catalog_id,
        details={
            "old_candidate_id": existing.get("candidate_id"),
            "new_candidate_id": candidate_id,
            "decided_by": decided_by,
            "reason": reason,
        },
    )
    return {"status": "approved", "idempotent": False, "old": existing, "new": new_row}


def resolve_challenge(
    store: DiscoveryStore,
    *,
    retailer: str,
    catalog_id: str,
    action: str,
    decided_by: str,
    mapping_path: str | Path,
    reason: str | None = None,
    now: str | None = None,
) -> dict[str, Any]:
    """Keep or replace the approved mapping for a pending challenge cell."""
    if action not in {"keep", "replace"}:
        raise ValueError("challenge action must be keep or replace")
    now = now or timestamp()
    with closing(store.connection()) as connection:
        connection.row_factory = sqlite3.Row
        cell = connection.execute(
            "SELECT * FROM discovery_cells "
            "WHERE retailer=? AND catalog_id=? AND state='review' "
            "AND review_category='challenge'",
            (retailer, catalog_id),
        ).fetchone()
    if cell is None:
        raise ValueError(f"no pending challenge for {retailer}/{catalog_id}")
    challenger_id = cell["candidate_id"]
    if not challenger_id:
        raise ValueError(f"challenge for {retailer}/{catalog_id} has no challenger candidate")

    if action == "keep":
        existing = approved_mapping(load_mappings(mapping_path), retailer, catalog_id)
        store.set_cell_state(
            retailer, catalog_id, "approved",
            candidate_id=existing.get("candidate_id") if existing else None,
            decided_by=decided_by,
            reason=reason or "challenge kept: existing mapping retained",
            changed_at=now,
        )
        store.diagnostic(
            event="challenge_kept",
            retailer=retailer, catalog_id=catalog_id,
            details={"challenger_candidate_id": challenger_id, "decided_by": decided_by},
        )
        return {
            "status": "kept",
            "retailer": retailer,
            "catalog_id": catalog_id,
            "challenger_candidate_id": challenger_id,
        }

    if not reason or not str(reason).strip():
        raise ValueError("replacement reason is required when resolving a challenge with replace")
    result = apply_mapping_replacement(
        store,
        retailer=retailer,
        catalog_id=catalog_id,
        candidate_id=challenger_id,
        mapping_path=mapping_path,
        decided_by=decided_by,
        reason=reason,
        now=now,
    )
    return {
        "status": "replaced",
        "retailer": retailer,
        "catalog_id": catalog_id,
        "challenger_candidate_id": challenger_id,
        "result": result,
    }


__all__ = [
    "apply_mapping_replacement", "decide_cell", "exact_match", "resolve_challenge",
]
