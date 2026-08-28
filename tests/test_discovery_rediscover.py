"""Tests for the term-expansion re-discovery pass (ticket 14).

Alternate search formulations (count/alias/size-explicit) are searched only
for the cells the classification pass marks thin, Class D, or still
inconclusive/unmapped.  All tests are hermetic: a temp DiscoveryStore, fake
adapters, no network.
"""

from __future__ import annotations

import contextlib
import io
import json
import os
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from unittest import mock

from beverage_feed.collector import BenchmarkPack
from beverage_feed.discovery import DiscoveryStore, load_mappings, write_mappings
from beverage_feed.discovery_adapters import (
    Capability,
    CapabilityContract,
    DiscoveryAdapter,
    DiscoveryResult,
    RequestEvent,
    normalize_listing,
)
from beverage_feed.discovery_run import (
    main as discovery_main,
    rediscovery_targets,
    run_discovery,
    run_rediscovery,
)


def pack(catalog_id="pack-1", search_term="Coca-Cola Zero Sugar") -> BenchmarkPack:
    return BenchmarkPack(
        catalog_id=catalog_id,
        name="Coca-Cola Zero Sugar 330ml Can x8",
        brand="Coca-Cola",
        variant="Zero Sugar",
        pack_count=8,
        unit_size_ml=330,
        package_type="can",
        search_term=search_term,
        aliases=("Coke Zero",),
    )


def other_pack(catalog_id: str, brand: str, variant: str, name: str) -> BenchmarkPack:
    """A distinct catalog pack, so attribute classification stays unambiguous."""
    return BenchmarkPack(
        catalog_id=catalog_id,
        name=name,
        brand=brand,
        variant=variant,
        pack_count=1,
        unit_size_ml=330,
        package_type="can",
        search_term=name,
    )


# No structured brand/variant: every pack-defining attribute must come from
# the listing name (plus the curated alias layer) so the evidence classifies
# as clean (Class A) in the re-classification pass.
EXACT_RECORD = {
    "productReference": "ref-1",
    "itemId": "item-1",
    "productName": "Coca-Cola Zero Sugar 330ml Can x8",
    "price": "9.50",
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


class RediscoveryTargetTests(unittest.TestCase):
    """Cell selection: thin, Class D, and unresolved-state cells only."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.store = DiscoveryStore(Path(self.tmp.name) / "discovery.sqlite")
        self.catalog = [
            pack("pack-1"),
            other_pack("pack-2", "Fanta", "Orange", "Fanta Orange 330ml Can"),
            other_pack("pack-3", "Pepsi", "Max", "Pepsi Max 330ml Can"),
        ]

    def _seed_evidence(self, candidate_id, catalog_id, *, price_status="valid"):
        self.store.upsert_candidate(
            candidate_id,
            retailer="dunnes",
            identity_key=candidate_id,
            identity_basis="test",
            identity_tier="product",
            source_product_name="Coca-Cola Zero Sugar 330ml Can x8",
        )
        self.store.associate_candidate(candidate_id, catalog_id, "term", retailer="dunnes")
        self.store.record_evidence(
            candidate_id, catalog_id,
            retailer="dunnes",
            normalized_attributes={
                "brand": "Coca-Cola", "variant": "Zero Sugar",
                "pack_count": 8, "unit_size_ml": 330, "package_type": "can",
            },
            inference_basis={key: "name" for key in (
                "brand", "variant", "pack_count", "unit_size_ml", "package_type",
            )},
            raw_price_value=None if price_status != "valid" else "9.50",
            price_parse_status=price_status,
        )

    def test_thin_class_d_and_unresolved_states_are_targets(self):
        # Class D: clean evidence but price missing.
        self._seed_evidence("c1", "pack-1", price_status="missing")
        self.store.set_cell_state("dunnes", "pack-1", "pending", decided_by="discovery")
        # Thin: no evidence at all.
        self.store.set_cell_state("dunnes", "pack-2", "inconclusive", decided_by="discovery")
        # Unresolved: classifiable evidence but the cell is still unmapped.
        self._seed_evidence("c3", "pack-3", price_status="malformed")
        self.store.set_cell_state("dunnes", "pack-3", "unmapped", decided_by="discovery")

        targets = rediscovery_targets(self.catalog, self.store)
        keys = {(t["retailer"], t["catalog_id"]) for t in targets}

        self.assertEqual(keys, {
            ("dunnes", "pack-1"), ("dunnes", "pack-2"), ("dunnes", "pack-3"),
        })
        reasons = {(t["retailer"], t["catalog_id"]): t["reason"] for t in targets}
        self.assertIn("class D", reasons[("dunnes", "pack-1")])
        self.assertIn("thin", reasons[("dunnes", "pack-2")])

    def test_decided_cells_are_never_targets(self):
        self._seed_evidence("c1", "pack-1", price_status="missing")
        self.store.set_cell_state("dunnes", "pack-1", "approved", decided_by="operator")

        targets = rediscovery_targets(self.catalog, self.store)

        self.assertEqual(
            [(t["retailer"], t["catalog_id"]) for t in targets], [],
        )

    def test_review_state_cell_with_evidence_is_not_a_target(self):
        # A review cell has classifiable evidence and a live decision state;
        # re-searching it would only duplicate the review queue.
        self._seed_evidence("c1", "pack-1")
        self.store.set_cell_state("dunnes", "pack-1", "review", decided_by="discovery")

        self.assertEqual(rediscovery_targets(self.catalog, self.store), [])


class RunRediscoveryTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.store = DiscoveryStore(Path(self.tmp.name) / "discovery.sqlite")

    def test_only_target_cells_are_searched(self):
        self.store.set_cell_state("dunnes", "pack-1", "inconclusive", decided_by="discovery")
        # pack-2 is approved: decided cells are out of scope.
        self.store.set_cell_state("dunnes", "pack-2", "approved", decided_by="operator")
        adapter = FakeAdapter()
        summary = run_rediscovery(
            [pack("pack-1"), pack("pack-2")], {"dunnes": adapter}, self.store,
        )

        self.assertEqual(len(adapter.calls), 4)  # 2 formulations x 1 target cell
        self.assertTrue(all("Coca-Cola Zero Sugar" in call or "Coke Zero" in call
                            for call in adapter.calls))
        self.assertEqual(summary["status"], "complete")
        kinds = [row[0] for row in self.store.connection().execute(
            "SELECT request_kind FROM discovery_search_history")]
        self.assertEqual(kinds, ["rediscovery"] * 4)

    def test_formulations_are_alternate_and_capped(self):
        self.store.set_cell_state("dunnes", "pack-1", "inconclusive", decided_by="discovery")
        adapter = FakeAdapter()
        run_rediscovery(
            [pack("pack-1")], {"dunnes": adapter}, self.store, max_formulations=2,
        )

        # Order: search term first, then alias — count/size formulations wait.
        self.assertEqual(adapter.calls, ["Coca-Cola Zero Sugar", "Coke Zero"])

    def test_already_searched_terms_are_not_reissued(self):
        adapter = FakeAdapter()
        run_discovery([pack("pack-1")], {"dunnes": adapter}, self.store)
        first_calls = list(adapter.calls)
        adapter.calls.clear()

        summary = run_rediscovery([pack("pack-1")], {"dunnes": adapter}, self.store)

        self.assertNotIn(first_calls[0], adapter.calls)
        # One unique formulation term (the original search term) was skipped.
        self.assertEqual(summary["skipped_searched"], 1)

    def test_exact_match_via_formulation_decides_cell_and_reclassifies(self):
        self.store.set_cell_state("dunnes", "pack-1", "inconclusive", decided_by="discovery")
        mapping_path = Path(self.tmp.name) / "mappings.json"
        write_mappings(mapping_path, {"dunnes": []})
        adapter = FakeAdapter({"Coke Zero 8 pack": result([EXACT_RECORD])})
        summary = run_rediscovery(
            [pack("pack-1")], {"dunnes": adapter}, self.store, mapping_path=mapping_path,
        )

        self.assertEqual(summary["auto_approved"], 1)
        state = self.store.connection().execute(
            "SELECT state FROM discovery_cells").fetchone()
        self.assertEqual(state, ("approved",))
        # Re-classification ran, and the new evidence is in the sprint batches.
        report = summary["classification"]
        self.assertEqual(report["counts"]["cells"]["A"], 1)
        batch = report["batches"]["A"]
        self.assertEqual(len(batch), 1)
        self.assertEqual(batch[0]["catalog_id"], "pack-1")

    def test_complete_results_without_exact_set_unmapped(self):
        self.store.set_cell_state("dunnes", "pack-1", "pending", decided_by="discovery")
        adapter = FakeAdapter()
        summary = run_rediscovery([pack("pack-1")], {"dunnes": adapter}, self.store)

        state = self.store.connection().execute(
            "SELECT state, reason FROM discovery_cells").fetchone()
        self.assertEqual(state[0], "unmapped")
        self.assertIn("rediscovery", state[1])
        self.assertEqual(summary["unmapped"], 1)

    def test_truncated_results_are_inconclusive(self):
        self.store.set_cell_state("dunnes", "pack-1", "pending", decided_by="discovery")
        adapter = FakeAdapter({term: result([], complete=False) for term in (
            "Coca-Cola Zero Sugar", "Coke Zero",
            "Coca-Cola Zero Sugar 8 pack", "Coke Zero 8 pack",
            "Coca-Cola Zero Sugar 330ml", "Coke Zero 330ml",
        )})
        summary = run_rediscovery([pack("pack-1")], {"dunnes": adapter}, self.store)

        state = self.store.connection().execute(
            "SELECT state FROM discovery_cells").fetchone()
        self.assertEqual(state, ("inconclusive",))
        self.assertEqual(summary["inconclusive"], 1)

    def test_request_cap_exhausts_retailer(self):
        self.store.set_cell_state("dunnes", "pack-1", "pending", decided_by="discovery")
        adapter = FakeAdapter()
        summary = run_rediscovery(
            [pack("pack-1")], {"dunnes": adapter}, self.store,
            request_caps={"dunnes": 2}, max_formulations=6,
        )

        self.assertEqual(summary["status"], "budget_exhausted")
        self.assertEqual(summary["retailers_exhausted"], ["dunnes"])
        self.assertLess(len(adapter.calls), 6)

    def test_source_failure_pauses_and_keeps_cell_pending(self):
        self.store.set_cell_state("dunnes", "pack-1", "pending", decided_by="discovery")
        adapter = FakeAdapter(error=RuntimeError("rate limited"))
        summary = run_rediscovery([pack("pack-1")], {"dunnes": adapter}, self.store)

        self.assertEqual(summary["status"], "paused")
        self.assertEqual(summary["failures"], 1)
        state = self.store.connection().execute(
            "SELECT state FROM discovery_cells").fetchone()
        self.assertEqual(state, ("pending",))

    def test_duplicate_formulations_are_deduplicated(self):
        # search_term already contains the count formulation; the pass must
        # not issue the same query twice for one cell.
        repeated = replace(
            pack("pack-1"), search_term="Coke Zero 8 pack",
        )
        self.store.set_cell_state("dunnes", "pack-1", "pending", decided_by="discovery")
        adapter = FakeAdapter()
        run_rediscovery([repeated], {"dunnes": adapter}, self.store)

        self.assertEqual(len(adapter.calls), len(set(adapter.calls)))


class RediscoveryCliTests(unittest.TestCase):
    """Exit-code contract of ``discovery --rediscover`` / ``--list-targets``."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.catalog = self.root / "catalog.json"
        self.catalog.write_text("[]\n")
        self.mapping_path = self.root / "mappings.json"
        self.rejection_path = self.root / "rejections.json"
        write_mappings(self.mapping_path, {"dunnes": []})
        self.base = [
            "--catalog", str(self.catalog),
            "--database", str(self.root / "feed.sqlite"),
            "--mapping", str(self.mapping_path),
            "--rejections", str(self.rejection_path),
        ]

    def test_list_targets_prints_json_without_retailer_access(self):
        stdout = io.StringIO()
        env = {key: "" for key in ("TESCO_API_KEY", "SUPERVALU_STORE_ID")}
        with mock.patch.dict(os.environ, env):
            with contextlib.redirect_stdout(stdout):
                code = discovery_main(["--list-targets", *self.base])

        self.assertEqual(code, 0)
        self.assertEqual(json.loads(stdout.getvalue()), [])

    def test_rediscover_completes_on_an_empty_catalog_without_tesco_key(self):
        stdout = io.StringIO()
        env = {key: "" for key in ("TESCO_API_KEY", "SUPERVALU_STORE_ID")}
        with mock.patch.dict(os.environ, env):
            with contextlib.redirect_stdout(stdout):
                code = discovery_main(["--rediscover", *self.base])

        self.assertEqual(code, 0)
        output = stdout.getvalue()
        self.assertIn("rediscovery complete", output)
        self.assertIn("evidence classification", output)

    def test_retailer_filter_narrows_rediscovery_targets(self):
        self.store = DiscoveryStore(self.root / "feed.sqlite")
        self.store.set_cell_state("dunnes", "pack-1", "pending", decided_by="discovery")
        self.store.set_cell_state("tesco", "pack-1", "pending", decided_by="discovery")
        stdout = io.StringIO()
        with contextlib.redirect_stdout(stdout):
            code = discovery_main(["--list-targets", "--retailer", "dunnes", *self.base])

        self.assertEqual(code, 0)
        targets = json.loads(stdout.getvalue())
        self.assertEqual(
            [(t["retailer"], t["catalog_id"]) for t in targets],
            [("dunnes", "pack-1")],
        )


if __name__ == "__main__":
    unittest.main()
