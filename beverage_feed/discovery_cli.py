"""Operator review, rejection, retry, and remapping operations.

Durable decisions live in ``data/mappings.json`` and ``data/rejections.json``
(JSON first, SQLite second).  Every transition is recorded so provenance is
never lost, and rejection history is superseded, never deleted.
"""

from __future__ import annotations

import argparse
import json
import sqlite3
from contextlib import closing
from datetime import timedelta
from pathlib import Path
from typing import Any

from .collector import as_datetime, timestamp
from .discovery import (
    DiscoveryStore,
    approved_mapping,
    candidate_id_for,
    load_mappings,
    load_rejections,
    reconcile_json_decisions,
    source_fields,
    write_mappings,
    write_rejections,
)

REVIEW_CATEGORIES = ("missing", "conflicting", "conflicting-candidates", "challenge")


def _candidate_row(store: DiscoveryStore, candidate_id: str) -> dict[str, Any]:
    with closing(store.connection()) as connection:
        connection.row_factory = sqlite3.Row
        row = connection.execute(
            "SELECT * FROM catalog_candidates WHERE candidate_id=?", (candidate_id,)
        ).fetchone()
    if row is None:
        raise ValueError(f"unknown Catalog Candidate: {candidate_id}")
    return dict(row)


def _candidate_for_cell(
    store: DiscoveryStore,
    candidate_id: str,
    retailer: str,
    catalog_id: str,
    *,
    require_evidence: bool,
) -> dict[str, Any]:
    candidate = _candidate_row(store, candidate_id)
    if candidate["retailer"] != retailer:
        raise ValueError(
            f"candidate {candidate_id} belongs to {candidate['retailer']}, not {retailer}"
        )
    with closing(store.connection()) as connection:
        associated = connection.execute(
            "SELECT 1 FROM discovery_candidate_cells "
            "WHERE candidate_id=? AND retailer=? AND catalog_id=?",
            (candidate_id, retailer, catalog_id),
        ).fetchone()
        if associated is None:
            raise ValueError(
                f"candidate {candidate_id} is not associated with {retailer}/{catalog_id}"
            )
        if require_evidence:
            evidence = connection.execute(
                "SELECT 1 FROM discovery_candidate_evidence "
                "WHERE candidate_id=? AND retailer=? AND catalog_id=? LIMIT 1",
                (candidate_id, retailer, catalog_id),
            ).fetchone()
            if evidence is None:
                raise ValueError(
                    f"candidate {candidate_id} has no evidence for {retailer}/{catalog_id}"
                )
    return candidate


def _rejected_listing_keys(
    rejections: dict[str, list[dict[str, Any]]],
    retailer: str,
    catalog_id: str,
) -> set[str]:
    return {
        row["canonical_key"]
        for row in rejections["listings"]
        if row["state"] == "rejected"
        and row["retailer"] == retailer
        and row.get("cell", row.get("catalog_id")) == catalog_id
    }


def review_list(
    store: DiscoveryStore,
    *,
    retailer: str | None = None,
    category: str | None = None,
) -> list[dict[str, Any]]:
    """List review cells with stored raw/normalized evidence and diffs."""
    if category is not None and category not in REVIEW_CATEGORIES:
        raise ValueError(f"unsupported review category: {category}")
    query = "SELECT * FROM discovery_cells WHERE state='review'"
    parameters: list[str] = []
    if retailer:
        query += " AND retailer=?"
        parameters.append(retailer)
    if category:
        query += " AND review_category=?"
        parameters.append(category)
    query += " ORDER BY retailer, catalog_id"
    results: list[dict[str, Any]] = []
    with closing(store.connection()) as connection:
        connection.row_factory = sqlite3.Row
        for cell in connection.execute(query, parameters):
            evidence = connection.execute(
                "SELECT candidate_id, raw_attributes, normalized_attributes, inference_basis, "
                "attribute_diffs, raw_price_value, price_parse_status, price_parse_reason "
                "FROM discovery_candidate_evidence WHERE retailer=? AND catalog_id=? "
                "ORDER BY evidence_id",
                (cell["retailer"], cell["catalog_id"]),
            ).fetchall()
            results.append({
                "retailer": cell["retailer"],
                "catalog_id": cell["catalog_id"],
                "review_category": cell["review_category"],
                "candidate_id": cell["candidate_id"],
                "reason": cell["reason"],
                "decided_by": cell["decided_by"],
                "decided_at": cell["decided_at"],
                "evidence": [dict(row) for row in evidence],
            })
    return results


def approve(
    store: DiscoveryStore,
    *,
    retailer: str,
    catalog_id: str,
    candidate_id: str,
    mapping_path: str | Path,
    rejection_path: str | Path,
    decided_by: str,
    reason: str | None = None,
    now: str | None = None,
) -> dict[str, Any]:
    """Operator-approve one candidate; resolves competing candidates for the cell."""
    now = now or timestamp()
    mappings = load_mappings(mapping_path)
    candidate = _candidate_for_cell(
        store, candidate_id, retailer, catalog_id, require_evidence=True,
    )
    rejected = _rejected_listing_keys(
        load_rejections(rejection_path), retailer, catalog_id,
    )
    if candidate_id in rejected:
        raise ValueError(
            "candidate is rejected for this cell; reset the rejection or choose another candidate first"
        )
    existing = approved_mapping(mappings, retailer, catalog_id)
    if existing is not None:
        if existing.get("candidate_id") == candidate_id:
            return {"status": "approved", "idempotent": True, "row": existing}
        raise ValueError("cell already has an approved mapping; revoke or replace it first")

    row = {
        "catalog_id": catalog_id,
        "expected_product_name": candidate["source_product_name"],
        "status": "approved",
        "decision_kind": "operator",
        "decided_by": decided_by,
        "decided_at": now,
        "matched_source_identity": candidate["identity_key"],
        "identity_tier": candidate["identity_tier"],
        "candidate_id": candidate_id,
        **({"decision_reason": reason} if reason else {}),
        **source_fields(retailer, candidate["identity_key"], candidate["identity_tier"]),
    }
    mappings.setdefault(retailer, []).append(row)
    write_mappings(mapping_path, mappings)  # durable JSON first, SQLite second
    store.set_cell_state(
        retailer, catalog_id, "approved",
        candidate_id=candidate_id, decided_by=decided_by, reason=reason,
    )
    with closing(store.connection()) as connection:
        connection.execute(
            "UPDATE catalog_candidates SET status='resolved' "
            "WHERE candidate_id <> ? AND status='pending_review' AND candidate_id IN "
            "(SELECT candidate_id FROM discovery_candidate_cells WHERE retailer=? AND catalog_id=?)",
            (candidate_id, retailer, catalog_id),
        )
        connection.commit()
    return {"status": "approved", "idempotent": False, "row": row}


def revoke(
    store: DiscoveryStore,
    *,
    retailer: str,
    catalog_id: str,
    mapping_path: str | Path,
    decided_by: str,
    reason: str | None = None,
    now: str | None = None,
) -> dict[str, Any]:
    """Revoke an approval back to review; provenance stays in the transition log."""
    now = now or timestamp()
    mappings = load_mappings(mapping_path)
    rows = mappings.get(retailer, [])
    existing = approved_mapping(mappings, retailer, catalog_id)
    if existing is None:
        raise ValueError("no approved mapping to revoke")
    rows.remove(existing)
    write_mappings(mapping_path, mappings)
    store.set_cell_state(
        retailer, catalog_id, "review",
        decided_by=decided_by,
        reason=f"revoked approval of {existing.get('candidate_id')}: {reason or 'no reason given'}",
    )
    store.diagnostic(
        event="approval_revoked", level="warning",
        retailer=retailer, catalog_id=catalog_id,
        message=reason, details=existing,
    )
    return {"status": "review", "revoked": existing}


def reject_listing(
    store: DiscoveryStore,
    *,
    retailer: str,
    candidate_id: str,
    catalog_id: str,
    mapping_path: str | Path,
    rejection_path: str | Path,
    decided_by: str,
    reason: str | None = None,
    now: str | None = None,
) -> dict[str, Any]:
    """Reject one canonical candidate identity; history is appended, never deleted."""
    now = now or timestamp()
    _candidate_for_cell(
        store, candidate_id, retailer, catalog_id, require_evidence=False,
    )
    existing = approved_mapping(load_mappings(mapping_path), retailer, catalog_id)
    if existing is not None and existing.get("candidate_id") == candidate_id:
        raise ValueError("candidate is an approved mapping; revoke the mapping first")
    record = {
        "canonical_key": candidate_id,
        "retailer": retailer,
        "catalog_id": catalog_id,
        "cell": catalog_id,
        "rejected_at": now,
        "decided_by": decided_by,
        "reason": reason,
        "state": "rejected",
    }
    rejections = load_rejections(rejection_path)
    existing_rejection = next(
        (
            row for row in rejections["listings"]
            if row["canonical_key"] == candidate_id
            and row["retailer"] == retailer
            and row.get("cell", row.get("catalog_id")) == catalog_id
            and row["state"] == "rejected"
        ),
        None,
    )
    if existing_rejection is not None:
        return {"status": "rejected", "idempotent": True, "record": existing_rejection}
    rejections["listings"].append(record)
    write_rejections(rejection_path, rejections)  # durable JSON first
    store.reject_candidate(
        retailer=retailer, candidate_id=candidate_id, catalog_id=catalog_id,
        decided_by=decided_by, reason=reason, rejected_at=now,
    )
    return {"status": "rejected", "record": record}


def do_not_map_cell(
    store: DiscoveryStore,
    *,
    retailer: str,
    catalog_id: str,
    rejection_path: str | Path,
    decided_by: str,
    reason: str | None = None,
    now: str | None = None,
) -> dict[str, Any]:
    """Explicitly exclude a retailer-pack cell.  Not an availability claim."""
    now = now or timestamp()
    record = {
        "retailer": retailer,
        "catalog_id": catalog_id,
        "cell": catalog_id,
        "rejected_at": now,
        "decided_by": decided_by,
        "reason": reason,
        "state": "do_not_map",
    }
    rejections = load_rejections(rejection_path)
    existing_rejection = next(
        (
            row for row in rejections["cells"]
            if row["retailer"] == retailer
            and row.get("cell", row.get("catalog_id")) == catalog_id
            and row["state"] == "do_not_map"
        ),
        None,
    )
    if existing_rejection is not None:
        return {"status": "do_not_map", "idempotent": True, "record": existing_rejection}
    rejections["cells"].append(record)
    write_rejections(rejection_path, rejections)
    store.set_cell_state(
        retailer, catalog_id, "do_not_map",
        decided_by=decided_by, reason=f"do_not_map: {reason or 'no reason given'}",
    )
    return {"status": "do_not_map", "record": record}


def _older_than(timestamp: str, days: int | None, now: str | None = None) -> bool:
    if days is None:
        return True
    return as_datetime(timestamp) <= as_datetime(now) - timedelta(days=days)


def reset_rejections(
    store: DiscoveryStore,
    *,
    rejection_path: str | Path,
    decided_by: str,
    retailer: str | None = None,
    older_than_days: int | None = None,
    now: str | None = None,
) -> int:
    """Retry/reset matching rejections by superseding them; history is retained."""
    now = now or timestamp()
    rejections = load_rejections(rejection_path)
    count = 0
    for section in ("listings", "cells"):
        for row in rejections[section]:
            if row["state"] == "superseded":
                continue
            if retailer is not None and row["retailer"] != retailer:
                continue
            if not _older_than(row["rejected_at"], older_than_days, now):
                continue
            row["state"] = "superseded"
            row["superseded_at"] = now
            count += 1
            if section == "listings":
                store.supersede_rejection("listings", row["canonical_key"], superseded_at=now)
                with closing(store.connection()) as connection:
                    connection.execute(
                        "UPDATE catalog_candidates SET status='pending_review' WHERE candidate_id=?",
                        (row["canonical_key"],),
                    )
                    connection.commit()
            else:
                store.supersede_rejection(
                    "cells", f"{row['retailer']}:{row.get('cell', row.get('catalog_id'))}",
                    superseded_at=now,
                )
                store.set_cell_state(
                    row["retailer"], row.get("cell", row["catalog_id"]), "pending",
                    decided_by=decided_by, reason="do_not_map reset",
                )
    write_rejections(rejection_path, rejections)
    return count


def reopen_reviews(
    store: DiscoveryStore,
    *,
    decided_by: str,
    retailer: str | None = None,
    category: str | None = None,
    older_than_days: int | None = None,
    now: str | None = None,
) -> int:
    """Reopen matching review cells so normal runs re-search and append evidence."""
    if category is not None and category not in REVIEW_CATEGORIES:
        raise ValueError(f"unsupported review category: {category}")
    now = now or timestamp()
    query = "SELECT retailer, catalog_id, decided_at FROM discovery_cells WHERE state='review'"
    parameters: list[str] = []
    if retailer:
        query += " AND retailer=?"
        parameters.append(retailer)
    if category:
        query += " AND review_category=?"
        parameters.append(category)
    with closing(store.connection()) as connection:
        connection.row_factory = sqlite3.Row
        cells = connection.execute(query, parameters).fetchall()
    count = 0
    for cell in cells:
        if not _older_than(cell["decided_at"] or now, older_than_days, now):
            continue
        store.set_cell_state(
            cell["retailer"], cell["catalog_id"], "pending",
            decided_by=decided_by, reason="review reopened for retry",
        )
        count += 1
    return count


def replace_mapping(
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
    candidate = _candidate_for_cell(
        store, candidate_id, retailer, catalog_id, require_evidence=True,
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


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Operator review and remapping commands")
    parser.add_argument("--database", type=Path, default=Path("feed.sqlite"))
    parser.add_argument("--mapping", type=Path, default=Path("data/mappings.json"))
    parser.add_argument("--rejections", type=Path, default=Path("data/rejections.json"))
    parser.add_argument("--decided-by", default="operator")
    subparsers = parser.add_subparsers(dest="command", required=True)

    p = subparsers.add_parser("review-list")
    p.add_argument("--retailer", choices=("dunnes", "supervalu", "tesco"))
    p.add_argument("--category", choices=REVIEW_CATEGORIES)

    p = subparsers.add_parser("approve")
    p.add_argument("--retailer", required=True, choices=("dunnes", "supervalu", "tesco"))
    p.add_argument("--catalog-id", required=True)
    p.add_argument("--candidate-id", required=True)
    p.add_argument("--reason")

    p = subparsers.add_parser("revoke")
    p.add_argument("--retailer", required=True, choices=("dunnes", "supervalu", "tesco"))
    p.add_argument("--catalog-id", required=True)
    p.add_argument("--reason")

    p = subparsers.add_parser("reject")
    p.add_argument("--retailer", required=True, choices=("dunnes", "supervalu", "tesco"))
    p.add_argument("--catalog-id", required=True)
    p.add_argument("--candidate-id", required=True)
    p.add_argument("--reason")

    p = subparsers.add_parser("do-not-map")
    p.add_argument("--retailer", required=True, choices=("dunnes", "supervalu", "tesco"))
    p.add_argument("--catalog-id", required=True)
    p.add_argument("--reason")

    p = subparsers.add_parser("retry-rejections")
    p.add_argument("--retailer", choices=("dunnes", "supervalu", "tesco"))
    p.add_argument("--older-than-days", type=int)

    p = subparsers.add_parser("retry-reviews")
    p.add_argument("--retailer", choices=("dunnes", "supervalu", "tesco"))
    p.add_argument("--category", choices=REVIEW_CATEGORIES)
    p.add_argument("--older-than-days", type=int)

    p = subparsers.add_parser("replace")
    p.add_argument("--retailer", required=True, choices=("dunnes", "supervalu", "tesco"))
    p.add_argument("--catalog-id", required=True)
    p.add_argument("--candidate-id", required=True)
    p.add_argument("--reason", required=True)

    args = parser.parse_args(argv)
    store = DiscoveryStore(args.database)
    reconcile_json_decisions(store.database, args.mapping, args.rejections)

    if args.command == "review-list":
        entries = review_list(store, retailer=args.retailer, category=args.category)
        print(json.dumps(entries, indent=2, default=str))
        return 0
    if args.command == "approve":
        result = approve(
            store, retailer=args.retailer, catalog_id=args.catalog_id,
            candidate_id=args.candidate_id, mapping_path=args.mapping,
            rejection_path=args.rejections, decided_by=args.decided_by, reason=args.reason,
        )
    elif args.command == "revoke":
        result = revoke(
            store, retailer=args.retailer, catalog_id=args.catalog_id,
            mapping_path=args.mapping, decided_by=args.decided_by, reason=args.reason,
        )
    elif args.command == "reject":
        result = reject_listing(
            store, retailer=args.retailer, catalog_id=args.catalog_id,
            candidate_id=args.candidate_id, mapping_path=args.mapping,
            rejection_path=args.rejections, decided_by=args.decided_by, reason=args.reason,
        )
    elif args.command == "do-not-map":
        result = do_not_map_cell(
            store, retailer=args.retailer, catalog_id=args.catalog_id,
            rejection_path=args.rejections, decided_by=args.decided_by, reason=args.reason,
        )
    elif args.command == "retry-rejections":
        count = reset_rejections(
            store, rejection_path=args.rejections, decided_by=args.decided_by,
            retailer=args.retailer, older_than_days=args.older_than_days,
        )
        result = {"superseded": count}
    elif args.command == "retry-reviews":
        count = reopen_reviews(
            store, decided_by=args.decided_by, retailer=args.retailer,
            category=args.category, older_than_days=args.older_than_days,
        )
        result = {"reopened": count}
    else:  # replace
        result = replace_mapping(
            store, retailer=args.retailer, catalog_id=args.catalog_id,
            candidate_id=args.candidate_id, mapping_path=args.mapping,
            decided_by=args.decided_by, reason=args.reason,
        )
    print(json.dumps(result, indent=2, default=str))
    return 0


__all__ = [
    "approve", "do_not_map_cell", "reject_listing", "reopen_reviews", "replace_mapping",
    "reset_rejections", "revoke", "review_list", "main",
]
