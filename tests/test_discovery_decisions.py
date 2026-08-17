import json
import sqlite3
import tempfile
import unittest
from contextlib import closing
from pathlib import Path

from beverage_feed.collector import BenchmarkPack
from beverage_feed.discovery import (
    DiscoveryStore,
    load_mappings,
    reconcile_json_decisions,
    write_mappings,
    write_rejections,
)
from beverage_feed.discovery_adapters import (
    Capability,
    CapabilityContract,
    DiscoveryAdapter,
    normalize_listing,
)
from beverage_feed.discovery_decisions import decide_cell


def make_pack(catalog_id="pack-1"):
    return BenchmarkPack(
        catalog_id=catalog_id,
        name="Coca-Cola Original Taste 330ml Can",
        brand="Coca-Cola",
        variant="Original Taste",
        pack_count=1,
        unit_size_ml=330,
        package_type="can",
        search_term="Coca-Cola Original",
    )


EXACT = {
    "productReference": "ref-1",
    "itemId": "item-1",
    "productName": "Coca-Cola Original Taste 330ml Can",
    "brand": "Coca-Cola",
    "variant": "Original Taste",
    "price": "1.40",
}


def listing(record=None, **overrides):
    record = dict(record or EXACT)
    record.update(overrides)
    return normalize_listing("dunnes", record)


class CollectableAdapter(DiscoveryAdapter):
    retailer = "dunnes"
    capabilities = CapabilityContract({
        "composite": Capability("composite", True, "fixture", "fixture path"),
    })

    def __init__(self):
        pass  # decisions never call the network client

    def search(self, pack):
        raise NotImplementedError


class NoPathAdapter(CollectableAdapter):
    capabilities = CapabilityContract({})


class DecideCellTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.store = DiscoveryStore(self.root / "discovery.sqlite")
        self.mapping_path = self.root / "mappings.json"
        self.rejection_path = self.root / "rejections.json"
        write_rejections(self.rejection_path, {"listings": [], "cells": []})
        self.pack = make_pack()
        self.adapter = CollectableAdapter()

    def decide(self, candidates, adapter=None, catalog_id="pack-1"):
        return decide_cell(
            self.store,
            retailer="dunnes",
            pack=make_pack(catalog_id),
            candidates=candidates,
            adapter=adapter or self.adapter,
            mapping_path=self.mapping_path,
            run_id="run-1",
        )

    def test_auto_approves_unique_collectable_exact_candidate_with_provenance(self):
        decision = self.decide([listing()])

        self.assertEqual(decision["decision"], "approved")
        mappings = load_mappings(self.mapping_path)
        row = mappings["dunnes"][0]
        self.assertEqual(row["status"], "approved")
        self.assertTrue(row["auto_approved"])
        self.assertEqual(row["discovery_run_id"], "run-1")
        self.assertEqual(row["matched_source_identity"], "ref-1:item-1")
        self.assertEqual(row["identity_tier"], "composite")
        self.assertEqual(row["candidate_id"], "dunnes:ref-1:item-1")
        self.assertEqual(row["source_product_reference"], "ref-1")
        self.assertEqual(row["source_item_id"], "item-1")
        self.assertTrue(row["decided_at"])
        state = self.store.connection().execute(
            "SELECT state, candidate_id FROM discovery_cells").fetchone()
        self.assertEqual(state, ("approved", "dunnes:ref-1:item-1"))

    def test_missing_evidence_never_auto_approves(self):
        # No variant evidence on the listing.
        decision = self.decide([listing(variant=None)])

        self.assertEqual(decision["decision"], "review")
        self.assertEqual(decision["category"], "missing")
        self.assertFalse(self.mapping_path.exists())

    def test_capability_rejection_routes_to_review(self):
        decision = self.decide([listing()], adapter=NoPathAdapter())

        self.assertEqual(decision["decision"], "review")
        self.assertEqual(decision["category"], "missing")
        self.assertFalse(self.mapping_path.exists())

    def test_multiple_exact_candidates_become_conflicting_candidates(self):
        a = listing()
        b = listing(productReference="ref-2", itemId="item-2")
        decision = self.decide([a, b])

        self.assertEqual(decision["decision"], "review")
        self.assertEqual(decision["category"], "conflicting-candidates")
        self.assertFalse(self.mapping_path.exists())

    def test_exact_duplicates_labeled_distinctly_from_divergent_attributes(self):
        # Same normalized attributes, different source identity -> duplicate.
        dup = self.decide([
            listing(),
            listing(productReference="ref-2", itemId="item-2"),
        ])
        self.assertIn("duplicates differing only by source identity", dup["reason"])

        # Same pack-defining attributes but a divergent structured total
        # volume -> divergent normalized attributes.
        divergent = self.decide([
            listing(),
            listing(productReference="ref-3", itemId="item-3", totalVolume="2000 ml"),
        ], catalog_id="pack-other")
        self.assertIn("divergent normalized attributes", divergent["reason"])

    def test_at_most_one_approved_mapping_per_cell(self):
        self.decide([listing()])
        # A second decision for the same cell must not add a second mapping.
        self.decide([listing()])
        rows = load_mappings(self.mapping_path)["dunnes"]
        self.assertEqual(len(rows), 1)

    def test_late_exact_candidate_creates_challenge_without_demoting(self):
        self.decide([listing()])  # approved mapping on ref-1:item-1
        decision = self.decide([listing(productReference="ref-9", itemId="item-9")])

        self.assertEqual(decision["decision"], "challenge")
        self.assertEqual(decision["category"], "challenge")
        rows = load_mappings(self.mapping_path)["dunnes"]
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["matched_source_identity"], "ref-1:item-1")
        state = self.store.connection().execute(
            "SELECT state, review_category FROM discovery_cells").fetchone()
        self.assertEqual(state, ("review", "challenge"))

    def test_capability_downgrade_keeps_existing_mapping_but_records_diagnostic(self):
        self.decide([listing()])  # approved with composite tier
        # Downgrade: adapter no longer supports composite.
        decision = self.decide(
            [listing(productReference="ref-9", itemId="item-9")],
            adapter=NoPathAdapter(),
        )
        self.assertEqual(decision["decision"], "challenge")
        rows = load_mappings(self.mapping_path)["dunnes"]
        self.assertEqual(len(rows), 1)  # not invalidated
        diag = self.store.connection().execute(
            "SELECT event FROM discovery_diagnostics WHERE event='capability_downgrade'"
        ).fetchone()
        self.assertIsNotNone(diag)

    def test_json_first_commit_is_repaired_after_interrupted_sqlite_write(self):
        # Simulate: JSON committed, SQLite cell write lost before commit.
        self.decide([listing()])
        with closing(self.store.connection()) as connection:
            connection.execute(
                "INSERT INTO catalog_packs VALUES "
                "('pack-1', 'x', 'Coca-Cola', 'Original Taste', 1, 330, 'can', 'x')"
            )
            connection.execute("DELETE FROM discovery_cells")
            connection.commit()

        reconcile_json_decisions(self.store.database, self.mapping_path, self.rejection_path)

        with closing(self.store.connection()) as connection:
            state = connection.execute(
                "SELECT state FROM discovery_cells").fetchone()
        self.assertEqual(state, ("approved",))

    def test_discovery_price_is_evidence_only_and_never_writes_observations(self):
        self.decide([listing()])
        observations = self.store.connection().execute(
            "SELECT COUNT(*) FROM price_observations").fetchone()[0]
        self.assertEqual(observations, 0)


if __name__ == "__main__":
    unittest.main()
