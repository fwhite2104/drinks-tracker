import tempfile
import unittest
from contextlib import closing
from pathlib import Path

from beverage_feed.discovery import (
    DiscoveryStore,
    load_mappings,
    load_rejections,
    reconcile_json_decisions,
    write_mappings,
    write_rejections,
)
from beverage_feed.discovery_cli import (
    approve,
    do_not_map_cell,
    reject_listing,
    reopen_reviews,
    replace_mapping,
    reset_rejections,
    revoke,
    review_list,
)


class ReviewCliTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.store = DiscoveryStore(self.root / "feed.sqlite")
        self.mapping_path = self.root / "mappings.json"
        self.rejection_path = self.root / "rejections.json"
        write_rejections(self.rejection_path, {"listings": [], "cells": []})
        with closing(self.store.connection()) as connection:
            connection.execute(
                "INSERT INTO catalog_packs VALUES "
                "('pack-1', 'Cola 330ml Can', 'Cola', 'Original', 1, 330, 'can', 'Cola')"
            )
            connection.commit()
        self.store.upsert_candidate(
            "dunnes:sku-1:item-1",
            retailer="dunnes", identity_key="sku-1:item-1",
            identity_basis="product_reference:item_id", identity_tier="composite",
            source_product_reference="sku-1", source_item_id="item-1",
            source_product_name="Cola 330ml Can",
        )
        self.store.upsert_candidate(
            "dunnes:sku-2:item-2",
            retailer="dunnes", identity_key="sku-2:item-2",
            identity_basis="product_reference:item_id", identity_tier="composite",
            source_product_reference="sku-2", source_item_id="item-2",
            source_product_name="Cola 330ml Can",
        )
        self.store.record_evidence(
            "dunnes:sku-1:item-1", "pack-1", retailer="dunnes",
            raw_attributes={"size": "330ml"}, normalized_attributes={"unit_size_ml": 330},
            inference_basis={"unit_size_ml": "name"}, attribute_diffs={},
            raw_price_value="1.20", price_parse_status="valid",
        )
        self.store.set_cell_state(
            "dunnes", "pack-1", "review", review_category="conflicting-candidates",
            candidate_id="dunnes:sku-1:item-1",
        )

    def test_review_list_filters_and_shows_evidence(self):
        entries = review_list(self.store, retailer="dunnes", category="conflicting-candidates")
        self.assertEqual(len(entries), 1)
        entry = entries[0]
        self.assertEqual(entry["review_category"], "conflicting-candidates")
        evidence = entry["evidence"][0]
        self.assertEqual(evidence["raw_attributes"], '{"size":"330ml"}')
        self.assertEqual(evidence["price_parse_status"], "valid")
        self.assertEqual(review_list(self.store, category="challenge"), [])

    def test_approve_records_operator_provenance_and_resolves_competition(self):
        result = approve(
            self.store, retailer="dunnes", catalog_id="pack-1",
            candidate_id="dunnes:sku-1:item-1", mapping_path=self.mapping_path,
            rejection_path=self.rejection_path, decided_by="alice", reason="checked by hand",
        )
        row = load_mappings(self.mapping_path)["dunnes"][0]
        self.assertEqual(row["decision_kind"], "operator")
        self.assertEqual(row["decided_by"], "alice")
        self.assertEqual(row["decision_reason"], "checked by hand")
        self.assertNotIn("auto_approved", row)
        state = self.store.connection().execute(
            "SELECT state FROM discovery_cells").fetchone()
        self.assertEqual(state, ("approved",))
        self.assertFalse(result["idempotent"])

        # Re-approval is idempotent and never adds a second mapping.
        again = approve(
            self.store, retailer="dunnes", catalog_id="pack-1",
            candidate_id="dunnes:sku-1:item-1", mapping_path=self.mapping_path,
            rejection_path=self.rejection_path, decided_by="alice",
        )
        self.assertTrue(again["idempotent"])
        self.assertEqual(len(load_mappings(self.mapping_path)["dunnes"]), 1)

    def test_approve_resolves_competing_candidates_and_checks_retailer(self):
        self.store.associate_candidate("dunnes:sku-1:item-1", "pack-1", "Cola")
        self.store.associate_candidate("dunnes:sku-2:item-2", "pack-1", "Cola")
        approve(
            self.store, retailer="dunnes", catalog_id="pack-1",
            candidate_id="dunnes:sku-1:item-1", mapping_path=self.mapping_path,
            rejection_path=self.rejection_path, decided_by="alice",
        )
        statuses = dict(self.store.connection().execute(
            "SELECT candidate_id, status FROM catalog_candidates").fetchall())
        self.assertEqual(statuses["dunnes:sku-1:item-1"], "pending_review")
        self.assertEqual(statuses["dunnes:sku-2:item-2"], "resolved")
        with self.assertRaises(ValueError):
            approve(
                self.store, retailer="tesco", catalog_id="pack-1",
                candidate_id="dunnes:sku-1:item-1", mapping_path=self.mapping_path,
                rejection_path=self.rejection_path, decided_by="alice",
            )

    def test_approving_a_second_candidate_requires_revoke_or_replace(self):
        approve(
            self.store, retailer="dunnes", catalog_id="pack-1",
            candidate_id="dunnes:sku-1:item-1", mapping_path=self.mapping_path,
            rejection_path=self.rejection_path, decided_by="alice",
        )
        with self.assertRaises(ValueError):
            approve(
                self.store, retailer="dunnes", catalog_id="pack-1",
                candidate_id="dunnes:sku-2:item-2", mapping_path=self.mapping_path,
                rejection_path=self.rejection_path, decided_by="alice",
            )

    def test_revoke_returns_cell_to_review_and_keeps_provenance(self):
        approve(
            self.store, retailer="dunnes", catalog_id="pack-1",
            candidate_id="dunnes:sku-1:item-1", mapping_path=self.mapping_path,
            rejection_path=self.rejection_path, decided_by="alice",
        )
        revoke(
            self.store, retailer="dunnes", catalog_id="pack-1",
            mapping_path=self.mapping_path, decided_by="bob", reason="wrong pack",
        )
        self.assertEqual(load_mappings(self.mapping_path).get("dunnes"), [])
        with closing(self.store.connection()) as connection:
            state = connection.execute("SELECT state FROM discovery_cells").fetchone()
            history = connection.execute(
                "SELECT to_state FROM discovery_state_transitions ORDER BY transition_id"
            ).fetchall()
        self.assertEqual(state, ("review",))
        self.assertIn(("approved",), history)  # provenance retained

    def test_listing_rejection_persists_durable_history(self):
        reject_listing(
            self.store, retailer="dunnes", candidate_id="dunnes:sku-2:item-2",
            catalog_id="pack-1", mapping_path=self.mapping_path,
            rejection_path=self.rejection_path, decided_by="alice", reason="wrong size",
            now="2025-01-01T00:00:00Z",
        )
        record = load_rejections(self.rejection_path)["listings"][0]
        self.assertEqual(record["canonical_key"], "dunnes:sku-2:item-2")
        self.assertEqual(record["rejected_at"], "2025-01-01T00:00:00Z")
        self.assertEqual(record["decided_by"], "alice")
        self.assertEqual(record["state"], "rejected")
        status = self.store.connection().execute(
            "SELECT status FROM catalog_candidates WHERE candidate_id='dunnes:sku-2:item-2'"
        ).fetchone()
        self.assertEqual(status, ("rejected",))

    def test_rejected_and_approved_cannot_coexist(self):
        approve(
            self.store, retailer="dunnes", catalog_id="pack-1",
            candidate_id="dunnes:sku-1:item-1", mapping_path=self.mapping_path,
            rejection_path=self.rejection_path, decided_by="alice",
        )
        with self.assertRaises(ValueError):
            reject_listing(
                self.store, retailer="dunnes", candidate_id="dunnes:sku-1:item-1",
                catalog_id="pack-1", mapping_path=self.mapping_path,
                rejection_path=self.rejection_path, decided_by="alice",
            )
        reject_listing(
            self.store, retailer="dunnes", candidate_id="dunnes:sku-2:item-2",
            catalog_id="pack-1", mapping_path=self.mapping_path,
            rejection_path=self.rejection_path, decided_by="alice",
        )
        with self.assertRaises(ValueError):
            approve(
                self.store, retailer="dunnes", catalog_id="pack-1",
                candidate_id="dunnes:sku-2:item-2", mapping_path=self.mapping_path,
                rejection_path=self.rejection_path, decided_by="alice",
            )

    def test_do_not_map_is_a_distinct_explicit_status(self):
        do_not_map_cell(
            self.store, retailer="tesco", catalog_id="pack-1",
            rejection_path=self.rejection_path, decided_by="alice", reason="never sold",
        )
        record = load_rejections(self.rejection_path)["cells"][0]
        self.assertEqual(record["state"], "do_not_map")
        state = self.store.connection().execute(
            "SELECT state FROM discovery_cells WHERE retailer='tesco'").fetchone()
        self.assertEqual(state, ("do_not_map",))

    def test_retry_rejections_supersedes_with_filters_and_keeps_history(self):
        for i, (candidate, when) in enumerate([
            ("dunnes:sku-2:item-2", "2024-01-01T00:00:00Z"),
            ("dunnes:sku-1:item-1", "2025-06-01T00:00:00Z"),
        ]):
            reject_listing(
                self.store, retailer="dunnes", candidate_id=candidate,
                catalog_id="pack-1", mapping_path=self.mapping_path,
                rejection_path=self.rejection_path, decided_by="alice", now=when,
            )
        count = reset_rejections(
            self.store, rejection_path=self.rejection_path, decided_by="bob",
            retailer="dunnes", older_than_days=90, now="2025-07-01T00:00:00Z",
        )
        self.assertEqual(count, 1)  # only the 2024 rejection is old enough
        rows = load_rejections(self.rejection_path)["listings"]
        self.assertEqual(len(rows), 2)  # history never deleted
        states = {row["canonical_key"]: row["state"] for row in rows}
        self.assertEqual(states["dunnes:sku-2:item-2"], "superseded")
        self.assertEqual(states["dunnes:sku-1:item-1"], "rejected")
        self.assertTrue(rows[0]["superseded_at"])

    def test_retry_reviews_reopens_with_filters(self):
        self.store.set_cell_state(
            "tesco", "pack-1", "review", review_category="challenge",
            changed_at="2024-01-01T00:00:00Z",
        )
        count = reopen_reviews(
            self.store, decided_by="bob", retailer="tesco",
            category="challenge", older_than_days=30, now="2025-07-01T00:00:00Z",
        )
        self.assertEqual(count, 1)
        states = dict(self.store.connection().execute(
            "SELECT retailer, state FROM discovery_cells").fetchall())
        self.assertEqual(states, {"dunnes": "review", "tesco": "pending"})
        # The original dunnes review (no filter match) was untouched.
        self.assertEqual(
            reopen_reviews(self.store, decided_by="bob", retailer="tesco"), 0
        )

    def test_replace_resolves_challenge_and_retains_old_mapping(self):
        approve(
            self.store, retailer="dunnes", catalog_id="pack-1",
            candidate_id="dunnes:sku-1:item-1", mapping_path=self.mapping_path,
            rejection_path=self.rejection_path, decided_by="alice",
        )
        result = replace_mapping(
            self.store, retailer="dunnes", catalog_id="pack-1",
            candidate_id="dunnes:sku-2:item-2", mapping_path=self.mapping_path,
            decided_by="bob", reason="supplier relisted the pack",
        )
        rows = load_mappings(self.mapping_path)["dunnes"]
        self.assertEqual(len(rows), 2)  # old mapping retained
        old, new = rows
        self.assertEqual(old["status"], "rejected")
        self.assertEqual(old["superseded_by"], "dunnes:sku-2:item-2")
        self.assertEqual(new["status"], "approved")
        self.assertEqual(new["candidate_id"], "dunnes:sku-2:item-2")
        self.assertEqual(result["old"]["matched_source_identity"], "sku-1:item-1")

        # JSON-first recovery: lose SQLite state, reconcile repairs it.
        with closing(self.store.connection()) as connection:
            connection.execute("DELETE FROM discovery_cells")
            connection.commit()
        reconcile_json_decisions(self.store.database, self.mapping_path, self.rejection_path)
        state = self.store.connection().execute(
            "SELECT state, candidate_id FROM discovery_cells").fetchone()
        self.assertEqual(state, ("approved", "dunnes:sku-2:item-2"))


if __name__ == "__main__":
    unittest.main()
