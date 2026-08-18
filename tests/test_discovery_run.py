import tempfile
import unittest
from pathlib import Path

from beverage_feed.collector import BenchmarkPack
from beverage_feed.discovery import DiscoveryStore, write_mappings, write_rejections
from beverage_feed.discovery_adapters import (
    Capability,
    CapabilityContract,
    DiscoveryAdapter,
    DiscoveryResult,
    RequestEvent,
    normalize_listing,
)
from beverage_feed.discovery_run import run_discovery


def pack(catalog_id="pack-1", search_term="Coca-Cola Original 330ml Can") -> BenchmarkPack:
    return BenchmarkPack(
        catalog_id=catalog_id,
        name="Coca-Cola Original Taste 330ml Can",
        brand="Coca-Cola",
        variant="Original Taste",
        pack_count=1,
        unit_size_ml=330,
        package_type="can",
        search_term=search_term,
    )


EXACT_RECORD = {
    "productReference": "ref-1",
    "itemId": "item-1",
    "productName": "Coca-Cola Original Taste 330ml Can",
    "brand": "Coca-Cola",
    "variant": "Original Taste",
    "price": "1.40",
}


def result(records, complete=True, retailer="dunnes", events=("search",)):
    listings = tuple(normalize_listing(retailer, record) for record in records)
    return DiscoveryResult(
        listings, complete, {}, tuple(records),
        tuple(RequestEvent(kind) for kind in events),
    )


class FakeAdapter(DiscoveryAdapter):
    retailer = "dunnes"
    max_requests_per_search = 1
    capabilities = CapabilityContract({
        "composite": Capability("composite", True, "fixture", "fixture path"),
    })

    def __init__(self, by_term=None, error=None):
        self.by_term = by_term or {}
        self.error = error
        self.calls = []

    def search(self, pack):
        self.calls.append(pack.search_term)
        if self.error:
            raise self.error
        return self.by_term.get(pack.search_term, result([]))


class DiscoveryRunTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.store = DiscoveryStore(Path(self.tmp.name) / "discovery.sqlite")

    def test_primary_exact_match_sets_review_and_records_history(self):
        adapter = FakeAdapter({"coke": result([EXACT_RECORD])})
        summary = run_discovery(
            [pack(search_term="coke")], {"dunnes": adapter}, self.store,
        )

        self.assertEqual(summary["status"], "complete")
        self.assertEqual(summary["cells_advanced"], 1)
        self.assertEqual(summary["candidates_found"], 1)
        state = self.store.connection().execute(
            "SELECT state, review_category, candidate_id FROM discovery_cells"
        ).fetchone()
        self.assertEqual(state, ("review", None, "dunnes:ref-1:item-1"))
        history = self.store.connection().execute(
            "SELECT search_term, request_kind, complete FROM discovery_search_history"
        ).fetchall()
        self.assertEqual(history, [("coke", "search", "true")])
        self.assertEqual(summary["request_counts"], {"search": 1})

    def test_fallback_runs_once_when_primary_complete_has_no_exact_candidate(self):
        adapter = FakeAdapter({
            "coke": result([{"productReference": "r", "itemId": "i", "productName": "Water 500ml"}]),
            "Coca-Cola Original Taste": result([EXACT_RECORD]),
        })
        summary = run_discovery([pack(search_term="coke")], {"dunnes": adapter}, self.store)

        self.assertEqual(adapter.calls, ["coke", "Coca-Cola Original Taste"])
        kinds = [row[0] for row in self.store.connection().execute(
            "SELECT request_kind FROM discovery_search_history ORDER BY search_id")]
        self.assertEqual(kinds, ["search", "fallback"])
        self.assertEqual(summary["status"], "complete")

    def test_complete_sets_without_exact_candidates_become_unmapped(self):
        adapter = FakeAdapter()
        summary = run_discovery([pack(search_term="coke")], {"dunnes": adapter}, self.store)

        state = self.store.connection().execute(
            "SELECT state FROM discovery_cells").fetchone()
        self.assertEqual(state, ("unmapped",))
        self.assertEqual(summary["cells_advanced"], 1)

    def test_truncated_primary_is_inconclusive_and_skips_fallback(self):
        adapter = FakeAdapter({"coke": result([], complete=False)})
        summary = run_discovery([pack(search_term="coke")], {"dunnes": adapter}, self.store)

        self.assertEqual(adapter.calls, ["coke"])
        state = self.store.connection().execute(
            "SELECT state FROM discovery_cells").fetchone()
        self.assertEqual(state, ("inconclusive",))
        self.assertEqual(summary["inconclusive"], 1)

    def test_request_cap_exhausts_retailer_and_keeps_cells_pending(self):
        adapter = FakeAdapter({"a": result([EXACT_RECORD]), "b": result([EXACT_RECORD])})
        summary = run_discovery(
            [pack("p1", "a"), pack("p2", "b")], {"dunnes": adapter}, self.store,
            request_caps={"dunnes": 1},
        )

        self.assertEqual(summary["status"], "budget_exhausted")
        self.assertEqual(summary["retailers_exhausted"], ["dunnes"])
        self.assertEqual(summary["pending"], 1)
        self.assertEqual(adapter.calls, ["a"])
        rows = dict(self.store.connection().execute(
            "SELECT catalog_id, state FROM discovery_cells").fetchall())
        self.assertEqual(rows, {"p1": "review"})  # p2 never searched, no row

    def test_resume_skips_terminal_and_review_cells_but_retouches_inconclusive(self):
        run_id = self.store.start_run()
        self.store.set_cell_state("dunnes", "done", "approved")
        self.store.set_cell_state("dunnes", "reviewed", "review")
        self.store.set_cell_state("dunnes", "soft", "inconclusive")
        adapter = FakeAdapter({"coke": result([EXACT_RECORD])})
        summary = run_discovery(
            [pack("done", "coke"), pack("reviewed", "coke"), pack("soft", "coke")],
            {"dunnes": adapter}, self.store,
            request_caps={"dunnes": 10}, run_id=run_id,
        )

        self.assertEqual(adapter.calls, ["coke"])
        self.assertTrue(summary["resumed"])
        rows = dict(self.store.connection().execute(
            "SELECT catalog_id, state FROM discovery_cells").fetchall())
        self.assertEqual(rows, {"done": "approved", "reviewed": "review", "soft": "review"})

    def test_new_invocation_preserves_review_state_and_does_not_research_it(self):
        mapping_path = Path(self.tmp.name) / "mappings.json"
        rejection_path = Path(self.tmp.name) / "rejections.json"
        write_mappings(mapping_path, {})
        write_rejections(rejection_path, {"listings": [], "cells": []})
        adapter = FakeAdapter({"coke": result([EXACT_RECORD])})
        adapter.is_collectable = lambda _listing: False

        run_discovery(
            [pack(search_term="coke")], {"dunnes": adapter}, self.store,
            mapping_path=mapping_path, rejection_path=rejection_path,
        )
        run_discovery(
            [pack(search_term="coke")], {"dunnes": adapter}, self.store,
            mapping_path=mapping_path, rejection_path=rejection_path,
        )

        self.assertEqual(adapter.calls, ["coke"])
        self.assertEqual(
            self.store.connection().execute(
                "SELECT state FROM discovery_cells"
            ).fetchone(),
            ("review",),
        )

    def test_source_failure_pauses_run_and_keeps_cell_pending(self):
        adapter = FakeAdapter(error=RuntimeError("rate limited"))
        summary = run_discovery([pack(search_term="coke")], {"dunnes": adapter}, self.store)

        self.assertEqual(summary["status"], "paused")
        self.assertEqual(summary["failures"], 1)
        state = self.store.connection().execute(
            "SELECT state, reason FROM discovery_cells").fetchone()
        self.assertEqual(state[0], "pending")
        self.assertIn("rate limited", state[1])

    def test_duplicate_search_terms_are_deduplicated_but_associations_accumulate(self):
        adapter = FakeAdapter({"coke": result([EXACT_RECORD])})
        summary = run_discovery(
            [pack("p1", "coke"), pack("p2", "coke")], {"dunnes": adapter}, self.store,
        )

        self.assertEqual(adapter.calls, ["coke"])
        self.assertEqual(summary["request_counts"], {"search": 1})
        terms = self.store.connection().execute(
            "SELECT candidate_id, retailer, search_term FROM discovery_candidate_search_terms"
        ).fetchall()
        self.assertEqual(len(terms), 1)
        associations = self.store.connection().execute(
            "SELECT COUNT(*) FROM discovery_candidate_cells").fetchone()[0]
        self.assertEqual(associations, 2)

    def test_rejected_listing_is_suppressed_and_does_not_block_absence(self):
        rejection_path = Path(self.tmp.name) / "rejections.json"
        write_rejections(rejection_path, {"listings": [{
            "canonical_key": "dunnes:ref-1:item-1",
            "retailer": "dunnes",
            "catalog_id": "pack-1",
            "rejected_at": "2025-01-01T00:00:00Z",
            "decided_by": "operator",
            "reason": "wrong pack",
            "state": "rejected",
        }], "cells": []})
        adapter = FakeAdapter({"coke": result([EXACT_RECORD])})
        summary = run_discovery(
            [pack(search_term="coke")], {"dunnes": adapter}, self.store,
            rejection_path=rejection_path,
        )

        self.assertEqual(summary["candidates_found"], 0)
        state = self.store.connection().execute(
            "SELECT state FROM discovery_cells").fetchone()
        self.assertEqual(state, ("unmapped",))


if __name__ == "__main__":
    unittest.main()
