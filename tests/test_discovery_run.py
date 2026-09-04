import contextlib
import io
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from beverage_feed.collector import BenchmarkPack
from beverage_feed.discovery import DiscoveryStore, load_mappings, write_mappings, write_rejections
from beverage_feed.discovery_adapters import (
    Capability,
    CapabilityContract,
    DiscoveryAdapter,
    DiscoveryResult,
    DunnesDiscoveryAdapter,
    RequestEvent,
    normalize_listing,
)
from beverage_feed.discovery_run import main as discovery_run_main, run_discovery


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
        adapter.capabilities = CapabilityContract({})

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


class DiscoveryCliMainTests(unittest.TestCase):
    """Exit-code contract of the ``discovery`` CLI dispatcher.

    An empty catalog means zero cells, so no adapter ever issues a request:
    the run completes without network access.
    """

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.catalog = self.root / "catalog.json"
        self.catalog.write_text("[]\n")
        self.mapping_path = self.root / "mappings.json"
        self.rejection_path = self.root / "rejections.json"
        write_mappings(self.mapping_path, {"dunnes": []})
        write_rejections(self.rejection_path, {"listings": [], "cells": []})
        self.base = [
            "--catalog", str(self.catalog),
            "--database", str(self.root / "feed.sqlite"),
            "--mapping", str(self.mapping_path),
            "--rejections", str(self.rejection_path),
            "--supervalu-store-id", "store-123",
            "--request-cap", "3",
        ]

    def test_main_completes_with_zero_requests_on_an_empty_catalog(self):
        stdout = io.StringIO()
        env = {"TESCO_API_KEY": "test-key", "SUPERVALU_STORE_ID": "store-123"}
        with mock.patch.dict(os.environ, env):
            with contextlib.redirect_stdout(stdout):
                code = discovery_run_main([*self.base])
        self.assertEqual(code, 0)
        output = stdout.getvalue()
        self.assertIn("discovery complete", output)
        self.assertIn("evaluated=0", output)
        self.assertIn("requests=-", output)
        output = stdout.getvalue()
        self.assertIn("discovery complete", output)
        self.assertIn("evaluated=0", output)
        self.assertIn("requests=-", output)

    def test_supervalu_without_a_store_id_is_a_usage_error(self):
        with mock.patch.dict(os.environ, {"SUPERVALU_STORE_ID": "", "TESCO_API_KEY": "test-key"}):
            with self.assertRaises(SystemExit) as ctx:
                discovery_run_main([
                    "--catalog", str(self.catalog),
                    "--database", str(self.root / "feed.sqlite"),
                    "--retailer", "supervalu",
                ])
        self.assertEqual(ctx.exception.code, 2)

    def test_walk_drinks_lists_the_pool_without_writing(self):
        """--walk-drinks sizes the Lidl Drinks candidate pool in list_only
        mode: JSON summary on stdout, no verdicts, no mappings written."""
        listings = tuple(
            normalize_listing("lidl", {
                "productId": str(100 + index), "name": f"Lidl Drink {index}",
                "price": "€1.39",
            })
            for index in range(3)
        )
        results = [(0, DiscoveryResult(listings, True, {}, (), ()))]
        client = mock.Mock()
        client.fetch_category_page.return_value = {"items": []}
        adapter = mock.Mock()
        adapter.walk_drinks.return_value = results
        with mock.patch("beverage_feed.lidl.LidlDiscoveryClient", return_value=client) as client_cls, \
                mock.patch("beverage_feed.discovery_adapters.LidlDiscoveryAdapter", return_value=adapter) as adapter_cls:
            adapter_cls.LIDL_DRINKS_CATEGORY = "10071022"
            stdout = io.StringIO()
            with contextlib.redirect_stdout(stdout):
                code = discovery_run_main([
                    "--catalog", str(self.catalog),
                    "--database", str(self.root / "feed.sqlite"),
                    "--retailer", "lidl",
                    "--walk-drinks",
                ])
        self.assertEqual(code, 0)
        summary = json.loads(stdout.getvalue())
        self.assertEqual(summary["retailer"], "lidl")
        self.assertEqual(summary["mode"], "list_only")
        self.assertEqual(summary["category"], "10071022")
        self.assertEqual(summary["listings"], 3)
        client_cls.assert_called_once_with()
        adapter_cls.assert_called_once_with(client)
        adapter.walk_drinks.assert_called_once()
        # Nothing durable: no mappings or rejections decisions were produced.
        self.assertEqual(load_mappings(self.mapping_path), {"dunnes": []})

    def test_walk_drinks_requires_the_lidl_retailer(self):
        with self.assertRaises(SystemExit) as ctx:
            discovery_run_main([
                "--catalog", str(self.catalog),
                "--database", str(self.root / "feed.sqlite"),
                "--walk-drinks",
            ])
        self.assertEqual(ctx.exception.code, 2)


if __name__ == "__main__":
    unittest.main()


class DietCokeAcceptanceTests(unittest.TestCase):
    """Ticket 12 acceptance, end to end and hermetic: the curated Brand Alias
    translation layer plus the junk gate, exercised through the real Dunnes
    discovery adapter and decision pipeline."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.store = DiscoveryStore(self.root / "discovery.sqlite")
        self.mapping_path = self.root / "mappings.json"
        write_mappings(self.mapping_path, {"dunnes": []})

    def diet_pack(self) -> BenchmarkPack:
        # The catalog's coca-diet-330-single entry.
        return BenchmarkPack(
            catalog_id="coca-diet-330-single",
            name="Coca-Cola Diet 330ml Can",
            brand="Coca-Cola",
            variant="Diet",
            pack_count=1,
            unit_size_ml=330,
            package_type="can",
            search_term="Diet Coke",
            aliases=("Diet Coke",),
        )

    @staticmethod
    def dunnes_payload() -> dict:
        # The envelope DunnesClient builds from the storefront gateway: the
        # exact can plus the junk a loose retail search drags in.
        def product(name, reference, price):
            return {
                "productName": name,
                "productReference": reference,
                "items": [{"itemId": reference, "sellers": [
                    {"commertialOffer": {"Price": price}}]}],
            }

        return {"data": {"productSearch": {"products": [
            product("Diet Coke 330ml Can", "100298012", "1.35"),
            product("POWERCUT Zip Hoodie Navy", "pc-hoodie", "24.00"),
            product("LED Desk Lamp 5W", "led-lamp", "9.00"),
        ]}}}

    def test_diet_coke_can_auto_approves_onto_its_cell_and_junk_does_not_attach(self):
        adapter = DunnesDiscoveryAdapter(lambda _: self.dunnes_payload())
        summary = run_discovery(
            [self.diet_pack()], {"dunnes": adapter}, self.store,
            mapping_path=self.mapping_path,
        )

        self.assertEqual(summary["status"], "complete")
        self.assertEqual(summary["auto_approved"], 1)
        state = self.store.connection().execute(
            "SELECT state FROM discovery_cells").fetchone()
        self.assertEqual(state, ("approved",))
        mappings = load_mappings(self.mapping_path)
        row = mappings["dunnes"][0]
        self.assertEqual(row["catalog_id"], "coca-diet-330-single")
        self.assertEqual(row["matched_source_identity"], "100298012:100298012")
        self.assertEqual(row["status"], "approved")

        # Junk gate: POWERCUT/LED-lamp listings stay canonical candidates but
        # never attach to the drink cell — no association, no evidence.
        associations = self.store.connection().execute(
            "SELECT candidate_id FROM discovery_candidate_cells").fetchall()
        self.assertEqual(associations, [("dunnes:100298012:100298012",)])
        evidence = self.store.connection().execute(
            "SELECT candidate_id FROM discovery_candidate_evidence").fetchall()
        self.assertEqual(evidence, [("dunnes:100298012:100298012",)])
