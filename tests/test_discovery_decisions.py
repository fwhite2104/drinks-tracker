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
from beverage_feed.discovery_cli import approve
from beverage_feed.discovery_decisions import decide_cell, resolve_challenge


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


class ChallengeResolutionTests(unittest.TestCase):
    """The keep/replace operator decision on a pending mapping challenge."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.store = DiscoveryStore(self.root / "feed.sqlite")
        self.mapping_path = self.root / "mappings.json"
        self.rejection_path = self.root / "rejections.json"
        write_rejections(self.rejection_path, {"listings": [], "cells": []})
        write_mappings(self.mapping_path, {"dunnes": []})
        self.store.upsert_candidate(
            "dunnes:sku-1:item-1", retailer="dunnes", identity_key="sku-1:item-1",
            identity_basis="product_reference:item_id", identity_tier="composite",
            source_product_reference="sku-1", source_item_id="item-1",
            source_product_name="Cola 330ml Can",
        )
        self.store.upsert_candidate(
            "dunnes:sku-2:item-2", retailer="dunnes", identity_key="sku-2:item-2",
            identity_basis="product_reference:item_id", identity_tier="composite",
            source_product_reference="sku-2", source_item_id="item-2",
            source_product_name="Cola 330ml Can",
        )
        for candidate_id in ("dunnes:sku-1:item-1", "dunnes:sku-2:item-2"):
            self.store.associate_candidate(candidate_id, "pack-1", "Cola")
        self.store.record_evidence(
            "dunnes:sku-1:item-1", "pack-1", retailer="dunnes",
            raw_attributes={"size": "330ml"}, normalized_attributes={"unit_size_ml": 330},
            inference_basis={"unit_size_ml": "name"}, attribute_diffs={},
            raw_price_value="1.20", price_parse_status="valid",
        )
        self.store.record_evidence(
            "dunnes:sku-2:item-2", "pack-1", retailer="dunnes",
            raw_attributes={"size": "330ml"}, normalized_attributes={"unit_size_ml": 330},
            inference_basis={"unit_size_ml": "name"}, attribute_diffs={},
            raw_price_value="1.25", price_parse_status="valid",
        )

    def _approve_then_challenge(self):
        approve(
            self.store, retailer="dunnes", catalog_id="pack-1",
            candidate_id="dunnes:sku-1:item-1", mapping_path=self.mapping_path,
            rejection_path=self.rejection_path, decided_by="alice",
        )
        # A late challenger surfaces: the cell reopens as a challenge without
        # demoting the approved mapping.
        self.store.set_cell_state(
            "dunnes", "pack-1", "review", review_category="challenge",
            candidate_id="dunnes:sku-2:item-2",
        )

    def test_keep_resolves_challenge_and_retains_existing_mapping(self):
        self._approve_then_challenge()
        result = resolve_challenge(
            self.store, retailer="dunnes", catalog_id="pack-1", action="keep",
            decided_by="bob", mapping_path=self.mapping_path,
        )
        self.assertEqual(result["status"], "kept")
        self.assertEqual(result["challenger_candidate_id"], "dunnes:sku-2:item-2")
        state = self.store.connection().execute(
            "SELECT state, candidate_id FROM discovery_cells "
            "WHERE retailer='dunnes' AND catalog_id='pack-1'"
        ).fetchone()
        self.assertEqual(state, ("approved", "dunnes:sku-1:item-1"))
        rows = load_mappings(self.mapping_path)["dunnes"]
        self.assertEqual(
            [(row["status"], row["candidate_id"]) for row in rows],
            [("approved", "dunnes:sku-1:item-1")],
        )
        event = self.store.connection().execute(
            "SELECT event FROM discovery_diagnostics "
            "WHERE retailer='dunnes' AND catalog_id='pack-1' AND event='challenge_kept'"
        ).fetchone()
        self.assertIsNotNone(event)

    def test_keep_without_an_existing_mapping_still_closes_the_challenge(self):
        self.store.set_cell_state(
            "dunnes", "pack-1", "review", review_category="challenge",
            candidate_id="dunnes:sku-2:item-2",
        )
        result = resolve_challenge(
            self.store, retailer="dunnes", catalog_id="pack-1", action="keep",
            decided_by="bob", mapping_path=self.mapping_path,
        )
        self.assertEqual(result["status"], "kept")
        state = self.store.connection().execute(
            "SELECT state, candidate_id FROM discovery_cells "
            "WHERE retailer='dunnes' AND catalog_id='pack-1'"
        ).fetchone()
        self.assertEqual(state, ("approved", None))
        self.assertEqual(load_mappings(self.mapping_path)["dunnes"], [])

    def test_replace_swaps_the_mapping_and_supersedes_the_old_candidate(self):
        self._approve_then_challenge()
        result = resolve_challenge(
            self.store, retailer="dunnes", catalog_id="pack-1", action="replace",
            decided_by="bob", mapping_path=self.mapping_path,
            reason="supplier relisted the pack",
        )
        self.assertEqual(result["status"], "replaced")
        rows = load_mappings(self.mapping_path)["dunnes"]
        by_status = {row["status"]: row for row in rows}
        self.assertEqual(by_status["approved"]["candidate_id"], "dunnes:sku-2:item-2")
        self.assertEqual(by_status["rejected"]["superseded_by"], "dunnes:sku-2:item-2")
        state = self.store.connection().execute(
            "SELECT state, candidate_id FROM discovery_cells "
            "WHERE retailer='dunnes' AND catalog_id='pack-1'"
        ).fetchone()
        self.assertEqual(state, ("approved", "dunnes:sku-2:item-2"))

    def test_replace_requires_a_reason(self):
        self._approve_then_challenge()
        with self.assertRaisesRegex(ValueError, "replacement reason is required"):
            resolve_challenge(
                self.store, retailer="dunnes", catalog_id="pack-1", action="replace",
                decided_by="bob", mapping_path=self.mapping_path, reason="   ",
            )

    def test_invalid_action_is_rejected(self):
        with self.assertRaisesRegex(ValueError, "keep or replace"):
            resolve_challenge(
                self.store, retailer="dunnes", catalog_id="pack-1", action="defer",
                decided_by="bob", mapping_path=self.mapping_path,
            )

    def test_resolving_a_cell_without_a_pending_challenge_is_rejected(self):
        with self.assertRaisesRegex(ValueError, "no pending challenge"):
            resolve_challenge(
                self.store, retailer="dunnes", catalog_id="pack-1", action="keep",
                decided_by="bob", mapping_path=self.mapping_path,
            )

    def test_challenge_without_a_challenger_candidate_is_rejected(self):
        self.store.set_cell_state(
            "dunnes", "pack-1", "review", review_category="challenge",
            candidate_id=None,
        )
        with self.assertRaisesRegex(ValueError, "no challenger candidate"):
            resolve_challenge(
                self.store, retailer="dunnes", catalog_id="pack-1", action="keep",
                decided_by="bob", mapping_path=self.mapping_path,
            )


if __name__ == "__main__":
    unittest.main()
