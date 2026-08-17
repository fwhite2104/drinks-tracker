"""Budgeted, resumable Catalog Mapping discovery orchestrator.

Discovery records evidence and mapping decisions only; it never writes a
Price Observation.  Collection (:mod:`beverage_feed.collector`) remains the
sole price-observation writer.
"""

from __future__ import annotations

import argparse
import os
from contextlib import closing
from pathlib import Path
from typing import Any, Mapping

from .collector import BenchmarkPack, DunnesClient, SuperValuClient, TescoClient, timestamp
from .discovery import (
    DiscoveryStore, candidate_id_for, load_rejections, reconcile_json_decisions,
)
from .discovery_adapters import DiscoveryAdapter, DiscoveryResult
from .discovery_decisions import exact_match, decide_cell

TERMINAL_STATES = {"approved", "rejected", "do_not_map", "unmapped"}
SKIPPED_STATES = TERMINAL_STATES | {"review"}
DEFAULT_REQUEST_CAP = 200

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
            cell_listings = [l for l in result.listings if candidate_id_for(retailer_name, l.source_identity) not in suppressed]

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
                    l for l in fallback.listings
                    if candidate_id_for(retailer_name, l.source_identity) not in suppressed
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


def _record_candidates(
    store: DiscoveryStore,
    summary: dict[str, Any],
    retailer_name: str,
    pack: BenchmarkPack,
    result: DiscoveryResult,
    search_term: str,
    suppressed: set[str],
) -> dict[str, Any]:
    """Persist canonical candidates and evidence; return exact matches."""
    exact: dict[str, Any] = {}
    for listing in result.listings:
        candidate_id = candidate_id_for(retailer_name, listing.source_identity)
        if candidate_id in suppressed:
            continue
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


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Run budgeted Catalog Mapping discovery")
    parser.add_argument("--catalog", type=Path, default=Path("data/catalog.json"))
    parser.add_argument("--mapping", type=Path, default=Path("data/mappings.json"))
    parser.add_argument("--rejections", type=Path, default=Path("data/rejections.json"))
    parser.add_argument("--database", type=Path, default=Path("feed.sqlite"))
    parser.add_argument("--retailer", choices=("dunnes", "supervalu", "tesco"))
    parser.add_argument(
        "--request-cap", type=int, default=DEFAULT_REQUEST_CAP,
        help="per-retailer outbound request cap for this run",
    )
    parser.add_argument("--run-id", help="resume an existing discovery run")
    parser.add_argument(
        "--supervalu-store-id",
        default=os.environ.get("SUPERVALU_STORE_ID"),
        help="configured SuperValu store identifier (or SUPERVALU_STORE_ID)",
    )
    args = parser.parse_args(argv)

    from .collector import load_catalog

    catalog = load_catalog(args.catalog)
    store = DiscoveryStore(args.database)

    retailers = [args.retailer] if args.retailer else ["dunnes", "supervalu", "tesco"]
    adapters: dict[str, DiscoveryAdapter] = {}
    from .discovery_adapters import (
        DunnesDiscoveryAdapter, SuperValuDiscoveryAdapter, TescoDiscoveryAdapter,
    )
    for name in retailers:
        if name == "dunnes":
            adapters[name] = DunnesDiscoveryAdapter(DunnesClient())
        elif name == "supervalu":
            if not args.supervalu_store_id:
                parser.error("--supervalu-store-id or SUPERVALU_STORE_ID is required for SuperValu")
            adapters[name] = SuperValuDiscoveryAdapter(SuperValuClient(args.supervalu_store_id))
        else:
            adapters[name] = TescoDiscoveryAdapter(TescoClient())

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


__all__ = ["run_discovery", "main", "DEFAULT_REQUEST_CAP"]
