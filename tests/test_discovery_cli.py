import contextlib
import io
import json
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
    challenge_list,
    do_not_map_cell,
    main as review_main,
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

    def test_decisions_require_the_candidate_to_belong_to_the_requested_cell(self):
        with self.assertRaisesRegex(ValueError, "not associated"):
            approve(
                self.store, retailer="dunnes", catalog_id="pack-2",
                candidate_id="dunnes:sku-1:item-1", mapping_path=self.mapping_path,
                rejection_path=self.rejection_path, decided_by="alice",
            )
        with self.assertRaisesRegex(ValueError, "not associated"):
            reject_listing(
                self.store, retailer="dunnes", catalog_id="pack-2",
                candidate_id="dunnes:sku-1:item-1", mapping_path=self.mapping_path,
                rejection_path=self.rejection_path, decided_by="alice",
            )

    def test_decisions_reject_a_candidate_whose_record_belongs_to_another_retailer(self):
        self.store.upsert_candidate(
            "dunnes:foreign:item", retailer="supervalu", identity_key="foreign",
            identity_basis="product_id", identity_tier="product",
            source_product_name="Cola 330ml Can",
        )
        self.store.associate_candidate("dunnes:foreign:item", "pack-1", "Cola")
        with self.assertRaisesRegex(ValueError, "belongs to supervalu"):
            approve(
                self.store, retailer="dunnes", catalog_id="pack-1",
                candidate_id="dunnes:foreign:item", mapping_path=self.mapping_path,
                rejection_path=self.rejection_path, decided_by="alice",
            )

    def test_approve_requires_evidence_for_the_requested_cell(self):
        self.store.associate_candidate("dunnes:sku-1:item-1", "pack-2", "Other Cola")
        with self.assertRaisesRegex(ValueError, "no evidence"):
            approve(
                self.store, retailer="dunnes", catalog_id="pack-2",
                candidate_id="dunnes:sku-1:item-1", mapping_path=self.mapping_path,
                rejection_path=self.rejection_path, decided_by="alice",
            )

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

    def test_multiple_candidates_in_one_cell_can_be_rejected_and_replayed(self):
        for candidate_id, when in (
            ("dunnes:sku-1:item-1", "2025-01-01T00:00:00Z"),
            ("dunnes:sku-2:item-2", "2025-01-02T00:00:00Z"),
        ):
            reject_listing(
                self.store, retailer="dunnes", candidate_id=candidate_id,
                catalog_id="pack-1", mapping_path=self.mapping_path,
                rejection_path=self.rejection_path, decided_by="alice", now=when,
            )

        self.assertEqual(len(load_rejections(self.rejection_path)["listings"]), 2)
        with closing(self.store.connection()) as connection:
            connection.execute("UPDATE catalog_candidates SET status='pending_review'")
            connection.commit()
        reconcile_json_decisions(
            self.store.database, self.mapping_path, self.rejection_path,
        )
        statuses = dict(self.store.connection().execute(
            "SELECT candidate_id, status FROM catalog_candidates"
        ).fetchall())
        self.assertEqual(set(statuses.values()), {"rejected"})
        self.assertEqual(
            self.store.connection().execute(
                "SELECT state FROM discovery_cells WHERE retailer='dunnes' AND catalog_id='pack-1'"
            ).fetchone(),
            ("rejected",),
        )

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
        again = reject_listing(
            self.store, retailer="dunnes", candidate_id="dunnes:sku-2:item-2",
            catalog_id="pack-1", mapping_path=self.mapping_path,
            rejection_path=self.rejection_path, decided_by="alice", reason="wrong size",
            now="2025-01-01T00:00:00Z",
        )
        self.assertTrue(again["idempotent"])
        self.assertEqual(len(load_rejections(self.rejection_path)["listings"]), 1)
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
        again = do_not_map_cell(
            self.store, retailer="tesco", catalog_id="pack-1",
            rejection_path=self.rejection_path, decided_by="alice", reason="never sold",
            now="2025-01-01T00:00:00Z",
        )
        self.assertTrue(again["idempotent"])
        self.assertEqual(len(load_rejections(self.rejection_path)["cells"]), 1)
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
        self.store.upsert_candidate(
            "dunnes:sku-3:item-3", retailer="dunnes", identity_key="sku-3:item-3",
            identity_basis="product_reference:item_id", identity_tier="composite",
            source_product_reference="sku-3", source_item_id="item-3",
            source_product_name="Cola 330ml Can",
        )
        self.store.associate_candidate("dunnes:sku-3:item-3", "pack-2", "Cola")
        self.store.record_evidence(
            "dunnes:sku-3:item-3", "pack-2", retailer="dunnes",
            raw_attributes={"size": "330ml"}, normalized_attributes={"unit_size_ml": 330},
            inference_basis={"unit_size_ml": "name"}, attribute_diffs={},
            raw_price_value="1.20", price_parse_status="valid",
        )
        with self.assertRaisesRegex(ValueError, "not associated"):
            replace_mapping(
                self.store, retailer="dunnes", catalog_id="pack-1",
                candidate_id="dunnes:sku-3:item-3", mapping_path=self.mapping_path,
                decided_by="bob", reason="wrong cell",
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


class ChallengeListingTests(unittest.TestCase):
    """The operator-facing side-by-side view of pending mapping challenges."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.store = DiscoveryStore(self.root / "feed.sqlite")
        self.mapping_path = self.root / "mappings.json"
        self.rejection_path = self.root / "rejections.json"
        write_rejections(self.rejection_path, {"listings": [], "cells": []})
        write_mappings(self.mapping_path, {"dunnes": []})
        with closing(self.store.connection()) as connection:
            connection.execute(
                "INSERT INTO catalog_packs VALUES "
                "('pack-1', 'Cola 330ml Can', 'Cola', 'Original', 1, 330, 'can', 'Cola')"
            )
            connection.commit()
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
            source_product_name="Cola Zero 330ml Can",
        )
        for candidate_id in ("dunnes:sku-1:item-1", "dunnes:sku-2:item-2"):
            self.store.associate_candidate(candidate_id, "pack-1", "Cola")
        self.store.record_evidence(
            "dunnes:sku-1:item-1", "pack-1", retailer="dunnes",
            raw_attributes={"size": "330ml"}, normalized_attributes={"unit_size_ml": 330},
            inference_basis={"unit_size_ml": "name"}, attribute_diffs={},
            raw_price_value="1.20", price_parse_status="valid",
        )

    def _challenge_cell(self):
        approve(
            self.store, retailer="dunnes", catalog_id="pack-1",
            candidate_id="dunnes:sku-1:item-1", mapping_path=self.mapping_path,
            rejection_path=self.rejection_path, decided_by="alice",
        )
        # Materialize the JSON decision into the SQLite mapping row, exactly
        # as every CLI entrypoint does before operating on the store.
        reconcile_json_decisions(self.store.database, self.mapping_path, self.rejection_path)
        self.store.set_cell_state(
            "dunnes", "pack-1", "review", review_category="challenge",
            candidate_id="dunnes:sku-2:item-2",
        )

    def test_challenge_rows_pair_the_approved_mapping_with_the_challenger(self):
        self._challenge_cell()
        self.store.record_evidence(
            "dunnes:sku-2:item-2", "pack-1", retailer="dunnes",
            raw_attributes={"size": "330ml", "name": "Cola Zero 330ml Can"},
            normalized_attributes={"unit_size_ml": 330},
            inference_basis={"unit_size_ml": "name"}, attribute_diffs={},
            raw_price_value="1.25", price_parse_status="valid",
        )
        rows = challenge_list(self.store, mapping_path=self.mapping_path)
        self.assertEqual(len(rows), 1)
        row = rows[0]
        self.assertEqual(row["retailer"], "dunnes")
        self.assertEqual(row["catalog_id"], "pack-1")
        self.assertEqual(row["challenger_candidate_id"], "dunnes:sku-2:item-2")
        self.assertEqual(row["approved_mapping"]["candidate_id"], "dunnes:sku-1:item-1")
        self.assertEqual(row["challenger_name"], "Cola Zero 330ml Can")
        self.assertEqual(row["challenger_attributes"], {"unit_size_ml": 330})
        # The pending challenge is stamped on the SQLite mapping row for
        # operators, idempotently and without touching approved status.
        with closing(self.store.connection()) as connection:
            stamped = connection.execute(
                "SELECT challenge_pending, status FROM catalog_mappings "
                "WHERE retailer='dunnes' AND catalog_id='pack-1'"
            ).fetchone()
        self.assertEqual(stamped, ("dunnes:sku-2:item-2", "approved"))
        # Listing is idempotent: a second read stamps the same challenger.
        challenge_list(self.store, mapping_path=self.mapping_path)
        with closing(self.store.connection()) as connection:
            stamped = connection.execute(
                "SELECT challenge_pending FROM catalog_mappings "
                "WHERE retailer='dunnes' AND catalog_id='pack-1'"
            ).fetchone()
        self.assertEqual(stamped, ("dunnes:sku-2:item-2",))

    def test_challenge_without_evidence_reports_none_attributes(self):
        self._challenge_cell()
        rows = challenge_list(self.store, mapping_path=self.mapping_path)
        self.assertIsNone(rows[0]["challenger_attributes"])
        self.assertIsNone(rows[0]["challenger_name"])

    def test_challenge_listing_filters_by_retailer(self):
        self._challenge_cell()
        self.store.set_cell_state(
            "tesco", "pack-1", "review", review_category="challenge",
            candidate_id="dunnes:sku-2:item-2",
        )
        rows = challenge_list(
            self.store, mapping_path=self.mapping_path, retailer="dunnes",
        )
        self.assertEqual([row["retailer"] for row in rows], ["dunnes"])


class ReviewCliMainTests(unittest.TestCase):
    """Exit-code and JSON-output contract of the ``review`` CLI dispatcher."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.store = DiscoveryStore(self.root / "feed.sqlite")
        self.database = self.root / "feed.sqlite"
        self.mapping_path = self.root / "mappings.json"
        self.rejection_path = self.root / "rejections.json"
        write_rejections(self.rejection_path, {"listings": [], "cells": []})
        write_mappings(self.mapping_path, {"dunnes": []})
        with closing(self.store.connection()) as connection:
            connection.execute(
                "INSERT INTO catalog_packs VALUES "
                "('pack-1', 'Cola 330ml Can', 'Cola', 'Original', 1, 330, 'can', 'Cola')"
            )
            connection.commit()
        self.store.upsert_candidate(
            "dunnes:sku-1:item-1", retailer="dunnes", identity_key="sku-1:item-1",
            identity_basis="product_reference:item_id", identity_tier="composite",
            source_product_reference="sku-1", source_item_id="item-1",
            source_product_name="Cola 330ml Can",
        )
        self.store.associate_candidate("dunnes:sku-1:item-1", "pack-1", "Cola")
        self.store.record_evidence(
            "dunnes:sku-1:item-1", "pack-1", retailer="dunnes",
            raw_attributes={"size": "330ml"}, normalized_attributes={"unit_size_ml": 330},
            inference_basis={"unit_size_ml": "name"}, attribute_diffs={},
            raw_price_value="1.20", price_parse_status="valid",
        )
        self.store.set_cell_state("dunnes", "pack-1", "pending")
        self.base = [
            "--database", str(self.database),
            "--mapping", str(self.mapping_path),
            "--rejections", str(self.rejection_path),
            "--decided-by", "alice",
        ]

    def test_review_list_exits_zero_and_prints_json(self):
        self.store.set_cell_state(
            "dunnes", "pack-1", "review", review_category="missing",
            candidate_id="dunnes:sku-1:item-1",
        )
        stdout = io.StringIO()
        with contextlib.redirect_stdout(stdout):
            code = review_main([*self.base, "review-list"])
        self.assertEqual(code, 0)
        entries = json.loads(stdout.getvalue())
        self.assertEqual(entries[0]["catalog_id"], "pack-1")

    def test_approve_dispatch_writes_the_mapping_and_closes_the_cell(self):
        stdout = io.StringIO()
        with contextlib.redirect_stdout(stdout):
            code = review_main([
                *self.base, "approve",
                "--retailer", "dunnes", "--catalog-id", "pack-1",
                "--candidate-id", "dunnes:sku-1:item-1",
            ])
        self.assertEqual(code, 0)
        rows = load_mappings(self.mapping_path)["dunnes"]
        self.assertEqual(rows[0]["candidate_id"], "dunnes:sku-1:item-1")
        state = self.store.connection().execute(
            "SELECT state FROM discovery_cells "
            "WHERE retailer='dunnes' AND catalog_id='pack-1'"
        ).fetchone()
        self.assertEqual(state, ("approved",))

    def test_challenges_without_resolve_prints_the_pending_list(self):
        approve(
            self.store, retailer="dunnes", catalog_id="pack-1",
            candidate_id="dunnes:sku-1:item-1", mapping_path=self.mapping_path,
            rejection_path=self.rejection_path, decided_by="alice",
        )
        self.store.set_cell_state(
            "dunnes", "pack-1", "review", review_category="challenge",
            candidate_id="dunnes:sku-1:item-1",
        )
        stdout = io.StringIO()
        with contextlib.redirect_stdout(stdout):
            code = review_main([*self.base, "challenges"])
        self.assertEqual(code, 0)
        challenges = json.loads(stdout.getvalue())
        self.assertEqual(challenges[0]["challenger_candidate_id"], "dunnes:sku-1:item-1")

    def test_missing_required_arguments_exit_with_usage_error(self):
        with self.assertRaises(SystemExit) as ctx:
            review_main([*self.base, "approve"])
        self.assertEqual(ctx.exception.code, 2)


if __name__ == "__main__":
    unittest.main()
