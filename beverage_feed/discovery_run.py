"""Budgeted, resumable Catalog Mapping discovery orchestrator.

Discovery records evidence and mapping decisions only; it never writes a
Price Observation.  Collection (:mod:`beverage_feed.collector`) remains the
sole price-observation writer.
"""

from __future__ import annotations

import argparse
import json
import os
import sqlite3
from contextlib import closing
from dataclasses import replace
from pathlib import Path
from typing import Any, Iterable, Mapping

from .collector import (
    AldiClient,
    BenchmarkPack,
    DunnesClient,
    LidlClient,
    SuperValuClient,
    TescoClient,
    timestamp,
)
from .discovery import (
    DiscoveryStore, candidate_id_for, load_rejections, reconcile_json_decisions,
)
from .discovery_adapters import DiscoveryAdapter, DiscoveryResult, NormalizedListing
from .discovery_classify import classify_evidence, format_classification
from .discovery_decisions import exact_match, decide_cell
from .matching import is_relevant_candidate, search_formulations

TERMINAL_STATES = {"approved", "rejected", "do_not_map", "unmapped"}
SKIPPED_STATES = TERMINAL_STATES | {"review"}
DEFAULT_REQUEST_CAP = 200

# Cell states the re-discovery pass treats as unresolved even when
# classification produced a class: the decision is still open.
REDISCOVERY_UNRESOLVED_STATES = {"pending", "inconclusive", "unmapped"}

# Politeness (ticket 11): Tesco discovery runs only via CI egress, so the
# default --rediscover retailer set never includes it; pass --retailer tesco
# explicitly from the CI workflow instead.
REDISCOVERY_DEFAULT_RETAILERS = (
    "dunnes",
    "supervalu",
)  # Tesco: CI egress only (ticket 11). Lidl/Aldi: deferred 2026-09-04 —
# their surfaces don't support automated price discovery; run explicitly
# with --retailer if that ever changes.
REDISCOVERY_MAX_FORMULATIONS = 4

_IDENTITY_BASIS = {
    "composite": "product_reference:item_id",
    "item": "item_id",
    "product": "product_id",
    "tpnb": "tpnb",
    "name_pack_signature": "name+pack_signature",
}


def _run_exists(store: DiscoveryStore, run_id: str) -> bool:
    with closing(store.connection()) as connection:
        return connection.execute(
            "SELECT 1 FROM discovery_runs WHERE run_id=?", (run_id,)
        ).fetchone() is not None


def _suppressed_candidates(rejection_path: str | Path | None) -> set[str]:
    if rejection_path is None or not Path(rejection_path).exists():
        return set()
    rejections = load_rejections(rejection_path)
    return {
        row["canonical_key"]
        for row in rejections["listings"]
        if row["state"] == "rejected"
    }


def _search_cost(adapter: DiscoveryAdapter) -> int:
    return adapter.max_requests_per_search + (0 if adapter.session_bootstrapped else 1)


def _charge(summary: dict[str, Any], result: DiscoveryResult) -> int:
    spent = 0
    for event in result.request_events:
        spent += 1
        summary["request_counts"][event.kind] = summary["request_counts"].get(event.kind, 0) + 1
        summary["batch_sizes"].setdefault(event.kind, []).append(event.batch_size)
    return spent


def run_discovery(
    catalog: list[BenchmarkPack],
    adapters: Mapping[str, DiscoveryAdapter],
    store: DiscoveryStore,
    *,
    mapping_path: str | Path | None = None,
    rejection_path: str | Path | None = None,
    retailer: str | None = None,
    request_caps: Mapping[str, int] | None = None,
    run_id: str | None = None,
) -> dict[str, Any]:
    """Evaluate one search per eligible unmapped retailer-pack cell.

    Retries once with the canonical brand-plus-variant fallback when the
    primary complete result set has no exact candidate.  Stops safely at
    per-retailer request caps or source failures; interrupted cells remain
    pending and resumable.
    """
    if mapping_path is not None and rejection_path is not None:
        reconcile_json_decisions(store.database, mapping_path, rejection_path)
    suppressed = _suppressed_candidates(rejection_path)
    caps = {name: DEFAULT_REQUEST_CAP for name in adapters}
    for name, cap in (request_caps or {}).items():
        if cap < 0:
            raise ValueError("request caps must not be negative")
        caps[name] = cap

    resumed = run_id is not None and _run_exists(store, run_id)
    run_id = run_id or store.start_run()
    attempt_id = store.start_attempt(run_id)
    started_at = timestamp()

    summary: dict[str, Any] = {
        "run_id": run_id,
        "attempt_id": attempt_id,
        "resumed": resumed,
        "started_at": started_at,
        "cells_evaluated": 0,
        "cells_advanced": 0,
        "auto_approved": 0,
        "challenges": 0,
        "candidates_found": 0,
        "pending": 0,
        "inconclusive": 0,
        "failures": 0,
        "request_counts": {},
        "batch_sizes": {},
        "retailers_exhausted": [],
    }
    selected = [name for name in adapters if retailer is None or name == retailer]
    search_cache: dict[tuple[str, str], DiscoveryResult] = {}
    paused = False

    for retailer_name in selected:
        adapter = adapters[retailer_name]
        cap = caps[retailer_name]
        spent = 0
        with closing(store.connection()) as connection:
            states = dict(connection.execute(
                "SELECT catalog_id, state FROM discovery_cells WHERE retailer=?",
                (retailer_name,),
            ).fetchall())
        for pack_index, pack in enumerate(catalog):
            if paused:
                break
            if states.get(pack.catalog_id) in SKIPPED_STATES:
                continue

            cost = _search_cost(adapter)
            if spent + cost > cap:
                summary["retailers_exhausted"].append(retailer_name)
                summary["pending"] += sum(
                    1 for remaining in catalog[pack_index:]
                    if states.get(remaining.catalog_id) not in SKIPPED_STATES
                )
                break

            summary["cells_evaluated"] += 1
            try:
                result, deduplicated = _search(adapter, retailer_name, pack, search_cache)
            except Exception as exc:
                summary["failures"] += 1
                store.set_cell_state(
                    retailer_name, pack.catalog_id, "pending",
                    decided_by="discovery", reason=f"source failure: {exc}",
                )
                store.diagnostic(
                    event="discovery_failure", level="error", message=str(exc),
                    run_id=run_id, attempt_id=attempt_id,
                    retailer=retailer_name, catalog_id=pack.catalog_id,
                )
                paused = True
                break

            spent += 0 if deduplicated else _charge(summary, result)
            store.record_search(
                run_id, attempt_id, pack.catalog_id, retailer_name, pack.search_term,
                complete=result.complete, request_kind="search",
                request_metadata={"deduplicated": deduplicated},
            )
            exact = _record_candidates(
                store, summary, retailer_name, pack, result, pack.search_term, suppressed,
            )
            cell_listings = _attached_listings(pack, result.listings, suppressed, retailer_name)

            if not exact and result.complete is True:
                fallback_term = f"{pack.brand} {pack.variant}"
                fallback_pack = BenchmarkPack(
                    catalog_id=pack.catalog_id, name=pack.name, brand=pack.brand,
                    variant=pack.variant, pack_count=pack.pack_count,
                    unit_size_ml=pack.unit_size_ml, package_type=pack.package_type,
                    search_term=fallback_term, aliases=pack.aliases,
                )
                cost = _search_cost(adapter)
                if spent + cost > cap:
                    summary["retailers_exhausted"].append(retailer_name)
                    summary["pending"] += 1
                    break
                try:
                    fallback, deduplicated = _search(adapter, retailer_name, fallback_pack, search_cache)
                except Exception as exc:
                    summary["failures"] += 1
                    store.set_cell_state(
                        retailer_name, pack.catalog_id, "pending",
                        decided_by="discovery", reason=f"fallback failure: {exc}",
                    )
                    paused = True
                    break
                spent += 0 if deduplicated else _charge(summary, fallback)
                store.record_search(
                    run_id, attempt_id, pack.catalog_id, retailer_name, fallback_term,
                    complete=fallback.complete, request_kind="fallback",
                    request_metadata={"deduplicated": deduplicated},
                )
                exact = _record_candidates(
                    store, summary, retailer_name, pack, fallback, fallback_term, suppressed,
                )
                cell_listings.extend(
                    _attached_listings(pack, fallback.listings, suppressed, retailer_name),
                )
                result = fallback

            if exact:
                decision = decide_cell(
                    store, retailer=retailer_name, pack=pack,
                    candidates=cell_listings, adapter=adapter,
                    mapping_path=mapping_path, run_id=run_id,
                )
                summary["cells_advanced"] += 1
                if decision["decision"] == "approved":
                    summary["auto_approved"] += 1
                elif decision["decision"] == "challenge":
                    summary["challenges"] += 1
            elif result.complete is True:
                store.set_cell_state(
                    retailer_name, pack.catalog_id, "unmapped",
                    decided_by="discovery",
                    reason="complete primary and fallback result sets contain no exact candidate",
                )
                summary["cells_advanced"] += 1
            else:
                store.set_cell_state(
                    retailer_name, pack.catalog_id, "inconclusive",
                    decided_by="discovery",
                    reason=f"result set completeness is {result.complete!r}",
                )
                summary["cells_advanced"] += 1
                summary["inconclusive"] += 1

    if paused:
        status = "paused"
    elif summary["retailers_exhausted"]:
        status = "budget_exhausted"
    else:
        status = "complete"
    summary["status"] = status
    summary["finished_at"] = timestamp()
    store.finish_attempt(
        run_id, attempt_id, status=status,
        request_counts=summary["request_counts"],
        batch_sizes={kind: max(sizes) for kind, sizes in summary["batch_sizes"].items()},
        cells_advanced=summary["cells_advanced"],
    )
    store.finish_run(
        run_id, status,
        request_counts=summary["request_counts"],
        batch_sizes={kind: max(sizes) for kind, sizes in summary["batch_sizes"].items()},
        cells_advanced=summary["cells_advanced"],
        summary=summary,
    )
    return summary


def rediscovery_targets(
    catalog: list[BenchmarkPack],
    store: DiscoveryStore,
    *,
    report: Mapping[str, Any] | None = None,
    retailer: str | None = None,
) -> list[dict[str, Any]]:
    """Cells the term-expansion re-discovery pass should search (ticket 14).

    The union of the classification report's rerun targets (thin cells and
    Class-D price-missing cells, ticket 13) and the still-pending,
    inconclusive, or unmapped cells that classification could not resolve
    into a decision.  Decided cells (approved/rejected/do_not_map) are never
    targets; review cells with classifiable evidence are not either.
    """
    classification = report if report is not None else classify_evidence(
        catalog, store, retailer=retailer,
    )
    targets: dict[tuple[str, str], dict[str, Any]] = {}
    for target in classification["rerun_targets"]:
        if retailer is not None and target["retailer"] != retailer:
            continue
        targets[(target["retailer"], target["catalog_id"])] = dict(target)
    with closing(store.connection()) as connection:
        connection.row_factory = sqlite3.Row
        rows = connection.execute("SELECT retailer, catalog_id, state FROM discovery_cells")
        for row in rows:
            key = (row["retailer"], row["catalog_id"])
            if row["state"] not in REDISCOVERY_UNRESOLVED_STATES or key in targets:
                continue
            if retailer is not None and row["retailer"] != retailer:
                continue
            targets[key] = {
                "retailer": key[0],
                "catalog_id": key[1],
                "state": row["state"],
                "reason": (
                    f"unresolved state {row['state']!r}: "
                    "classification could not decide the cell"
                ),
            }
    return [targets[key] for key in sorted(targets)]


def run_rediscovery(
    catalog: list[BenchmarkPack],
    adapters: Mapping[str, DiscoveryAdapter],
    store: DiscoveryStore,
    *,
    targets: Iterable[Mapping[str, Any]] | None = None,
    mapping_path: str | Path | None = None,
    rejection_path: str | Path | None = None,
    retailer: str | None = None,
    request_caps: Mapping[str, int] | None = None,
    run_id: str | None = None,
    max_formulations: int = REDISCOVERY_MAX_FORMULATIONS,
    reclassify: bool = True,
) -> dict[str, Any]:
    """Term-expansion re-discovery pass over thin target cells (ticket 14).

    Searches only the cells in *targets* (by default
    :func:`rediscovery_targets`: thin/Class-D cells from the classification
    pass plus still-unresolved cells).  Each target cell is searched with
    alternate search formulations (:func:`matching.search_formulations`) up
    to *max_formulations* per cell, skipping queries already recorded in the
    cell's search history so no retailer request is wasted repeating the
    original pass.  Per-retailer request caps and failure pausing work as in
    :func:`run_discovery`; searches are recorded with request kind
    ``rediscovery``.

    When *reclassify* is set, the evidence classification pass re-runs after
    the searches so the new evidence lands in the sprint batches.
    """
    if mapping_path is not None and rejection_path is not None:
        reconcile_json_decisions(store.database, mapping_path, rejection_path)
    suppressed = _suppressed_candidates(rejection_path)
    caps = {name: DEFAULT_REQUEST_CAP for name in adapters}
    for name, cap in (request_caps or {}).items():
        if cap < 0:
            raise ValueError("request caps must not be negative")
        caps[name] = cap

    target_map: dict[tuple[str, str], str] = {}
    resolved_targets = targets if targets is not None else rediscovery_targets(
        catalog, store, retailer=retailer,
    )
    for target in resolved_targets:
        key = (target["retailer"], target["catalog_id"])
        if retailer is not None and key[0] != retailer:
            continue
        target_map[key] = str(target.get("reason", "operator-supplied target"))

    resumed = run_id is not None and _run_exists(store, run_id)
    run_id = run_id or store.start_run()
    attempt_id = store.start_attempt(run_id)
    started_at = timestamp()

    summary: dict[str, Any] = {
        "run_id": run_id,
        "attempt_id": attempt_id,
        "pass": "rediscovery",
        "resumed": resumed,
        "started_at": started_at,
        "target_cells": len(target_map),
        "max_formulations": max_formulations,
        "cells_evaluated": 0,
        "cells_advanced": 0,
        "auto_approved": 0,
        "challenges": 0,
        "candidates_found": 0,
        "formulation_searches": 0,
        "skipped_searched": 0,
        "pending": 0,
        "inconclusive": 0,
        "unmapped": 0,
        "failures": 0,
        "request_counts": {},
        "batch_sizes": {},
        "retailers_exhausted": [],
    }

    # Politeness: never re-issue a query the cell's search history already
    # holds, regardless of which pass recorded it.
    searched: dict[tuple[str, str], set[str]] = {}
    with closing(store.connection()) as connection:
        connection.row_factory = sqlite3.Row
        for row in connection.execute(
            "SELECT retailer, catalog_id, search_term FROM discovery_search_history"
        ):
            key = (row["retailer"], row["catalog_id"])
            if key in target_map:
                searched.setdefault(key, set()).add(row["search_term"].strip().lower())

    selected = [name for name in adapters if retailer is None or name == retailer]
    search_cache: dict[tuple[str, str], DiscoveryResult] = {}
    paused = False

    for retailer_name in selected:
        adapter = adapters[retailer_name]
        cap = caps[retailer_name]
        spent = 0
        for pack_index, pack in enumerate(catalog):
            key = (retailer_name, pack.catalog_id)
            if key not in target_map:
                continue
            all_terms = search_formulations(pack)
            new_terms = [
                term for term in all_terms
                if term.strip().lower() not in searched.get(key, set())
            ]
            summary["skipped_searched"] += len(all_terms) - len(new_terms)
            if not new_terms:
                continue
            formulations = new_terms[:max_formulations]
            summary["cells_evaluated"] += 1

            cell_exact: dict[str, Any] = {}
            cell_listings: list[NormalizedListing] = []
            complete_seen = False
            for term in formulations:
                cost = _search_cost(adapter)
                if spent + cost > cap:
                    if retailer_name not in summary["retailers_exhausted"]:
                        summary["retailers_exhausted"].append(retailer_name)
                    summary["pending"] += 1 + sum(
                        1 for remaining in catalog[pack_index + 1:]
                        if (retailer_name, remaining.catalog_id) in target_map
                    )
                    break
                term_pack = replace(pack, search_term=term)
                try:
                    result, deduplicated = _search(
                        adapter, retailer_name, term_pack, search_cache,
                    )
                except Exception as exc:
                    summary["failures"] += 1
                    store.set_cell_state(
                        retailer_name, pack.catalog_id, "pending",
                        decided_by="discovery", reason=f"rediscovery failure: {exc}",
                    )
                    store.diagnostic(
                        event="rediscovery_failure", level="error", message=str(exc),
                        run_id=run_id, attempt_id=attempt_id,
                        retailer=retailer_name, catalog_id=pack.catalog_id,
                    )
                    paused = True
                    break

                spent += 0 if deduplicated else _charge(summary, result)
                summary["formulation_searches"] += 1
                store.record_search(
                    run_id, attempt_id, pack.catalog_id, retailer_name, term,
                    complete=result.complete, request_kind="rediscovery",
                    request_metadata={"deduplicated": deduplicated},
                )
                cell_exact.update(_record_candidates(
                    store, summary, retailer_name, pack, result, term, suppressed,
                ))
                cell_listings.extend(
                    _attached_listings(pack, result.listings, suppressed, retailer_name),
                )
                complete_seen = complete_seen or result.complete is True

            if paused:
                break
            if "retailers_exhausted" in summary and \
                    retailer_name in summary["retailers_exhausted"]:
                break

            summary["cells_advanced"] += 1
            if cell_exact:
                decision = decide_cell(
                    store, retailer=retailer_name, pack=pack,
                    candidates=cell_listings, adapter=adapter,
                    mapping_path=mapping_path, run_id=run_id,
                )
                if decision["decision"] == "approved":
                    summary["auto_approved"] += 1
                elif decision["decision"] == "challenge":
                    summary["challenges"] += 1
            elif complete_seen:
                store.set_cell_state(
                    retailer_name, pack.catalog_id, "unmapped",
                    decided_by="discovery",
                    reason="rediscovery complete result sets contain no exact candidate",
                )
                summary["unmapped"] += 1
            else:
                store.set_cell_state(
                    retailer_name, pack.catalog_id, "inconclusive",
                    decided_by="discovery",
                    reason="rediscovery result sets are incomplete for every formulation",
                )
                summary["inconclusive"] += 1

    if paused:
        status = "paused"
    elif summary["retailers_exhausted"]:
        status = "budget_exhausted"
    else:
        status = "complete"
    summary["status"] = status
    summary["finished_at"] = timestamp()
    store.finish_attempt(
        run_id, attempt_id, status=status,
        request_counts=summary["request_counts"],
        batch_sizes={kind: max(sizes) for kind, sizes in summary["batch_sizes"].items()},
        cells_advanced=summary["cells_advanced"],
    )
    store.finish_run(
        run_id, status,
        request_counts=summary["request_counts"],
        batch_sizes={kind: max(sizes) for kind, sizes in summary["batch_sizes"].items()},
        cells_advanced=summary["cells_advanced"],
        summary=summary,
    )
    if reclassify:
        # Re-classify so the new evidence lands in the sprint batches
        # (ticket 14: re-classify the new evidence after the pass).
        summary["classification"] = classify_evidence(catalog, store)
    return summary


def _search(
    adapter: DiscoveryAdapter,
    retailer_name: str,
    pack: BenchmarkPack,
    cache: dict[tuple[str, str], DiscoveryResult],
) -> tuple[DiscoveryResult, bool]:
    """Search one cell, deduplicating identical retailer/query calls."""
    key = (retailer_name, pack.search_term.strip().lower())
    if key in cache:
        return cache[key], True
    result = adapter.search(pack)
    cache[key] = result
    return result, False


def _attached_listings(
    pack: BenchmarkPack,
    listings: Iterable[NormalizedListing],
    suppressed: set[str],
    retailer_name: str,
) -> list[NormalizedListing]:
    """Listings that may attach to the cell: unsuppressed and junk-gated.

    Universal junk gate (CONTEXT.md: Catalog Mapping Discovery): a candidate
    attaches to a cell only when its name shares at least one brand/identity
    token with the search term or pack, so POWERCUT/LED-lamp-class listings
    surfaced by loose retailer searches never become cell evidence.
    """
    return [
        listing for listing in listings
        if candidate_id_for(retailer_name, listing.source_identity) not in suppressed
        and is_relevant_candidate(listing.name, pack)
    ]


def _record_candidates(
    store: DiscoveryStore,
    summary: dict[str, Any],
    retailer_name: str,
    pack: BenchmarkPack,
    result: DiscoveryResult,
    search_term: str,
    suppressed: set[str],
) -> dict[str, Any]:
    """Persist canonical candidates and evidence; return exact matches.

    Every surfaced listing is upserted as a canonical candidate (candidate
    identity is cell-independent), but only junk-gated, unsuppressed listings
    attach to the cell (association + evidence).
    """
    exact: dict[str, Any] = {}
    attached = _attached_listings(pack, result.listings, suppressed, retailer_name)
    for listing in result.listings:
        candidate_id = candidate_id_for(retailer_name, listing.source_identity)
        reference, _, item = listing.source_identity.partition(":")
        store.upsert_candidate(
            candidate_id,
            retailer=retailer_name,
            identity_key=listing.source_identity,
            identity_basis=_IDENTITY_BASIS.get(listing.identity_tier, listing.identity_tier),
            identity_tier=listing.identity_tier,
            source_product_reference=reference,
            source_item_id=item,
            source_product_name=listing.name,
            raw_record=listing.raw_record,
            displayed_price=(
                str(listing.price.raw_value) if listing.price.status == "valid" else None
            ),
        )
        if listing not in attached:
            continue
        store.associate_candidate(candidate_id, pack.catalog_id, search_term, retailer=retailer_name)
        store.record_evidence(
            candidate_id, pack.catalog_id,
            retailer=retailer_name,
            raw_attributes=listing.raw_attributes,
            normalized_attributes=listing.attributes,
            inference_basis=listing.inference_basis,
            attribute_diffs=listing.conflicts,
            raw_price_value=listing.price.raw_value,
            price_parse_status=listing.price.status,
            price_parse_reason=listing.price.reason,
        )
        summary["candidates_found"] += 1
        if exact_match(pack, listing):
            exact[candidate_id] = listing
    return exact


def _build_adapter(name: str, supervalu_store_id: str | None) -> DiscoveryAdapter:
    """Construct the discovery adapter for one retailer name."""
    from .discovery_adapters import (
        AldiDiscoveryAdapter,
        DunnesDiscoveryAdapter,
        LidlDiscoveryAdapter,
        SuperValuDiscoveryAdapter,
        TescoDiscoveryAdapter,
    )

    if name == "dunnes":
        return DunnesDiscoveryAdapter(DunnesClient())
    if name == "supervalu":
        if not supervalu_store_id:
            raise ValueError(
                "--supervalu-store-id or SUPERVALU_STORE_ID is required for SuperValu"
            )
        return SuperValuDiscoveryAdapter(SuperValuClient(supervalu_store_id))
    if name == "tesco":
        return TescoDiscoveryAdapter(TescoClient())
    if name == "lidl":
        return LidlDiscoveryAdapter(LidlClient())
    return AldiDiscoveryAdapter(AldiClient())


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Run budgeted Catalog Mapping discovery")
    parser.add_argument("--catalog", type=Path, default=Path("data/catalog.json"))
    parser.add_argument("--mapping", type=Path, default=Path("data/mappings.json"))
    parser.add_argument("--rejections", type=Path, default=Path("data/rejections.json"))
    parser.add_argument(
        "--database",
        type=Path,
        default=Path(os.environ.get("DRINKS_DATABASE", "data/feed.sqlite")),
    )
    parser.add_argument("--retailer", choices=("dunnes", "supervalu", "tesco", "lidl", "aldi"))
    parser.add_argument(
        "--request-cap", type=int, default=DEFAULT_REQUEST_CAP,
        help="per-retailer outbound request cap for this run",
    )
    parser.add_argument("--run-id", help="resume an existing discovery run")
    parser.add_argument(
        "--rediscover", action="store_true",
        help="term-expansion re-discovery pass over thin/Class-D/unresolved cells",
    )
    parser.add_argument(
        "--max-formulations", type=int, default=REDISCOVERY_MAX_FORMULATIONS,
        help="maximum alternate search formulations per cell in --rediscover mode",
    )
    parser.add_argument(
        "--list-targets", action="store_true",
        help="print the rediscovery target cells as JSON and exit (no retailer requests)",
    )
    parser.add_argument(
        "--walk-drinks", action="store_true",
        help="list-only Drinks category walk (Lidl only): print the candidate pool "
             "as JSON and exit; no verdicts, no mappings written, and bounded by "
             "--request-cap pages",
    )
    parser.add_argument(
        "--supervalu-store-id",
        default=os.environ.get("SUPERVALU_STORE_ID"),
        help="configured SuperValu store identifier (or SUPERVALU_STORE_ID)",
    )
    args = parser.parse_args(argv)

    if args.walk_drinks:
        # List-only path: no catalog, store or schema write is touched — the
        # walk prints evidence JSON and exits (review: nothing durable here).
        if args.retailer != "lidl":
            parser.error("--walk-drinks requires --retailer lidl")
        from .discovery_adapters import LidlDiscoveryAdapter
        from .lidl import LidlDiscoveryClient

        client = LidlDiscoveryClient()
        adapter = LidlDiscoveryAdapter(client)
        category = LidlDiscoveryAdapter.LIDL_DRINKS_CATEGORY
        # One outbound request per page, so the request cap bounds the walk.
        pages = adapter.walk_drinks(
            lambda offset: client.fetch_category_page(offset, category),
            max_pages=args.request_cap,
        )
        print(json.dumps({
            "retailer": "lidl",
            "mode": "list_only",
            "category": category,
            "pages": [
                {
                    "offset": offset,
                    "complete": result.complete,
                    "listings": len(result.listings),
                }
                for offset, result in pages
            ],
            "listings": sum(len(result.listings) for _, result in pages),
        }, indent=2))
        return 0

    from .collector import load_catalog

    catalog = load_catalog(args.catalog)
    store = DiscoveryStore(args.database)

    if args.list_targets:
        targets = rediscovery_targets(catalog, store, retailer=args.retailer)
        print(json.dumps(targets, indent=2, default=str))
        return 0

    if args.rediscover:
        targets = rediscovery_targets(catalog, store, retailer=args.retailer)
        target_retailers = {target["retailer"] for target in targets}
        if args.retailer:
            retailers = [args.retailer] if args.retailer in target_retailers else []
        else:
            # Politeness (ticket 11): Tesco discovery runs only via CI egress,
            # so it is excluded from the default re-discovery pass; run it
            # explicitly with --retailer tesco from the CI workflow.
            retailers = [
                name for name in REDISCOVERY_DEFAULT_RETAILERS if name in target_retailers
            ]
        adapters: dict[str, DiscoveryAdapter] = {}
        try:
            for name in retailers:
                adapters[name] = _build_adapter(name, args.supervalu_store_id)
        except ValueError as exc:
            parser.error(str(exc))

        summary = run_rediscovery(
            catalog, adapters, store,
            targets=targets,
            mapping_path=args.mapping,
            rejection_path=args.rejections,
            retailer=args.retailer,
            request_caps={name: args.request_cap for name in adapters},
            run_id=args.run_id,
            max_formulations=args.max_formulations,
        )
        requests = ",".join(
            f"{kind}={count}" for kind, count in sorted(summary["request_counts"].items())
        ) or "-"
        print(
            f"rediscovery {summary['status']}: run={summary['run_id']} "
            f"attempt={summary['attempt_id']} "
            f"targets={summary['target_cells']} "
            f"evaluated={summary['cells_evaluated']} "
            f"advanced={summary['cells_advanced']} "
            f"auto_approved={summary['auto_approved']} "
            f"searches={summary['formulation_searches']} "
            f"skipped_searched={summary['skipped_searched']} "
            f"inconclusive={summary['inconclusive']} "
            f"unmapped={summary['unmapped']} "
            f"failures={summary['failures']} "
            f"requests={requests}"
        )
        print(format_classification(summary["classification"]))
        return 0 if summary["status"] == "complete" else 1

    retailers = [args.retailer] if args.retailer else ["dunnes", "supervalu"]
    # Tesco: CI egress only (ticket 11 — home IP is Akamai-blocked).
    # Lidl/Aldi: deferred 2026-09-04 (operator decision) — no easy price
    # surface for automated discovery; explicit --retailer only.
    adapters = {}
    try:
        for name in retailers:
            adapters[name] = _build_adapter(name, args.supervalu_store_id)
    except ValueError as exc:
        parser.error(str(exc))

    summary = run_discovery(
        catalog, adapters, store,
        mapping_path=args.mapping,
        rejection_path=args.rejections,
        retailer=args.retailer,
        request_caps={name: args.request_cap for name in adapters},
        run_id=args.run_id,
    )
    requests = ",".join(f"{kind}={count}" for kind, count in sorted(summary["request_counts"].items())) or "-"
    exhausted = ",".join(summary["retailers_exhausted"]) or "-"
    print(
        f"discovery {summary['status']}: run={summary['run_id']} "
        f"attempt={summary['attempt_id']} "
        f"finished={summary['finished_at']} "
        f"evaluated={summary['cells_evaluated']} "
        f"advanced={summary['cells_advanced']} "
        f"candidates={summary['candidates_found']} "
        f"pending={summary['pending']} "
        f"inconclusive={summary['inconclusive']} "
        f"failures={summary['failures']} "
        f"requests={requests} "
        f"exhausted_retailers={exhausted}"
    )
    return 0 if summary["status"] == "complete" else 1


__all__ = [
    "run_discovery", "run_rediscovery", "rediscovery_targets", "main",
    "DEFAULT_REQUEST_CAP", "REDISCOVERY_MAX_FORMULATIONS",
]
