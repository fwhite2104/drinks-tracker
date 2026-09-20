import json
import sqlite3
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


class DiscoveryStateTests(unittest.TestCase):
    def test_atomic_json_writes_are_validated_and_stably_formatted(self):
        mappings = {
            "dunnes": [{
                "catalog_id": "pack-1",
                "expected_product_name": "Cola 330ml Can",
                "source_product_reference": "sku-1",
                "source_item_id": "item-1",
                "status": "approved",
            }]
        }
        rejections = {
            "listings": [{
                "canonical_key": "dunnes:sku-2:item-2",
                "retailer": "dunnes",
                "catalog_id": "pack-1",
                "rejected_at": "2025-01-01T00:00:00Z",
                "decided_by": "operator",
                "reason": "wrong pack",
                "state": "rejected",
            }],
            "cells": [],
            "retailer_blocks": [],
        }
        with tempfile.TemporaryDirectory() as directory:
            mapping_path = Path(directory) / "mappings.json"
            rejection_path = Path(directory) / "rejections.json"
            write_mappings(mapping_path, mappings)
            write_rejections(rejection_path, rejections)

            self.assertEqual(load_mappings(mapping_path), mappings)
            self.assertEqual(load_rejections(rejection_path), rejections)
            self.assertEqual(
                mapping_path.read_text(),
                json.dumps(mappings, indent=2, sort_keys=True) + "\n",
            )

            with self.assertRaises(ValueError):
                write_mappings(mapping_path, {"dunnes": [{"catalog_id": "bad"}]})

    def test_load_rejects_malformed_mapping_and_rejection_files(self):
        # Load-side validation: a hand-edited JSON file with an extra field or
        # a bad row must fail loudly (CONTRIBUTING §8), not load silently.
        with tempfile.TemporaryDirectory() as directory:
            mapping_path = Path(directory) / "mappings.json"
            rejection_path = Path(directory) / "rejections.json"
            mapping_path.write_text(json.dumps({
                "dunnes": [{
                    "catalog_id": "pack-1",
                    "expected_product_name": "Cola 330ml Can",
                    "status": "approved",
                    "source_notes": "hand-added field the schema never defined",
                }]
            }))
            rejection_path.write_text(json.dumps({
                "listings": [{
                    "canonical_key": "dunnes:sku-1:item-1",
                    "retailer": "dunnes",
                    "catalog_id": "pack-1",
                    "rejected_at": "2025-01-01T00:00:00Z",
                    "decided_by": "operator",
                    "state": "withdrawn",
                }],
                "cells": [],
            }))

            with self.assertRaises(ValueError):
                load_mappings(mapping_path)
            with self.assertRaises(ValueError):
                load_rejections(rejection_path)

    def test_store_persists_candidates_associations_history_and_state(self):
        with tempfile.TemporaryDirectory() as directory:
            database = Path(directory) / "discovery.sqlite"
            store = DiscoveryStore(database)
            store.start_run("run-1", started_at="2025-01-01T00:00:00Z")
            store.start_attempt("run-1", "attempt-1", started_at="2025-01-01T00:00:01Z")
            store.upsert_candidate(
                "dunnes:sku-1:item-1",
                retailer="dunnes",
                identity_key="sku-1:item-1",
                identity_basis="product_reference:item_id",
                identity_tier="composite",
                source_product_reference="sku-1",
                source_item_id="item-1",
                source_product_name="Cola 330ml Can",
                raw_record={"title": "Cola 330ml Can"},
            )
            store.upsert_candidate(
                "dunnes:sku-2:item-2",
                retailer="dunnes",
                identity_key="sku-2:item-2",
                identity_basis="product_reference:item_id",
                identity_tier="composite",
                source_product_reference="sku-2",
                source_item_id="item-2",
                source_product_name="Cola 330ml Can",
            )
            store.associate_candidate("dunnes:sku-1:item-1", "pack-1", "Cola 330ml Can")
            store.associate_candidate("dunnes:sku-2:item-2", "pack-1", "Cola 330ml Can")
            store.record_search(
                "run-1", "attempt-1", "pack-1", "dunnes", "Cola 330ml",
                complete=True, request_metadata={"kind": "search"},
            )
            store.record_evidence(
                "dunnes:sku-1:item-1", "pack-1", retailer="dunnes", raw_attributes={"size": "330ml"},
                normalized_attributes={"unit_size_ml": 330},
                inference_basis={"unit_size_ml": "structured"},
                attribute_diffs={}, raw_price_value="€1.20",
                price_parse_status="valid", price_parse_reason=None,
            )
            store.set_cell_state("dunnes", "pack-1", "review", review_category="conflicting-candidates")
            store.link_identity("dunnes", "dunnes:sku-1:item-1", "dunnes:sku-1:item-1-v2")
            store.reject_candidate(retailer="dunnes", candidate_id="dunnes:sku-1:item-1", catalog_id="pack-1", decided_by="operator")
            self.assertEqual(store.supersede_rejection("listings", "dunnes:sku-1:item-1"), 1)
            store.finish_attempt("run-1", "attempt-1", cells_advanced=1)
            store.finish_run("run-1", "paused")

            with sqlite3.connect(database) as connection:
                self.assertEqual(connection.execute("SELECT COUNT(*) FROM catalog_candidates").fetchone()[0], 2)
                self.assertEqual(connection.execute("SELECT COUNT(*) FROM discovery_candidate_cells").fetchone()[0], 2)
                self.assertEqual(connection.execute("SELECT state, review_category FROM discovery_cells").fetchone(), ("review", "conflicting-candidates"))
                self.assertEqual(connection.execute("SELECT status FROM discovery_runs").fetchone()[0], "paused")
                self.assertEqual(connection.execute("SELECT state FROM discovery_rejections").fetchone()[0], "superseded")
                self.assertEqual(connection.execute("SELECT COUNT(*) FROM discovery_identity_links").fetchone()[0], 1)
                self.assertEqual(connection.execute("SELECT COUNT(*) FROM price_observations").fetchone()[0], 0)

    def test_reconciliation_preserves_operational_cells_and_is_idempotent(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            database = root / "discovery.sqlite"
            mapping_path = root / "mappings.json"
            rejection_path = root / "rejections.json"
            write_mappings(mapping_path, {})
            write_rejections(rejection_path, {"listings": [], "cells": []})
            store = DiscoveryStore(database)
            store.set_cell_state("dunnes", "review", "review", review_category="missing")
            store.set_cell_state("dunnes", "pending", "pending")
            store.set_cell_state("supervalu", "inconclusive", "inconclusive")
            store.set_cell_state("tesco", "unmapped", "unmapped")

            reconcile_json_decisions(database, mapping_path, rejection_path)
            first = store.connection().execute(
                "SELECT retailer, catalog_id, state, review_category "
                "FROM discovery_cells ORDER BY retailer, catalog_id"
            ).fetchall()
            reconcile_json_decisions(database, mapping_path, rejection_path)
            second = store.connection().execute(
                "SELECT retailer, catalog_id, state, review_category "
                "FROM discovery_cells ORDER BY retailer, catalog_id"
            ).fetchall()

        self.assertEqual(second, first)
        self.assertEqual(
            first,
            [
                ("dunnes", "pending", "pending", None),
                ("dunnes", "review", "review", "missing"),
                ("supervalu", "inconclusive", "inconclusive", None),
                ("tesco", "unmapped", "unmapped", None),
            ],
        )

    def test_reconciliation_replays_explicit_json_decisions(self):
        mappings = {"dunnes": [{
            "catalog_id": "approved-pack",
            "expected_product_name": "Cola 330ml Can",
            "source_product_reference": "sku-1",
            "source_item_id": "item-1",
            "status": "approved",
        }]}
        rejections = {"listings": [{
            "canonical_key": "dunnes:sku-3:item-3",
            "retailer": "dunnes",
            "catalog_id": "rejected-pack",
            "rejected_at": "2025-01-01T00:00:00Z",
            "decided_by": "operator",
            "reason": "wrong pack",
            "state": "rejected",
        }], "cells": [{
            "retailer": "tesco",
            "catalog_id": "excluded-pack",
            "rejected_at": "2025-01-01T00:00:00Z",
            "decided_by": "operator",
            "reason": "not comparable",
            "state": "do_not_map",
        }]}
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            mapping_path = root / "mappings.json"
            rejection_path = root / "rejections.json"
            write_mappings(mapping_path, mappings)
            write_rejections(rejection_path, rejections)
            store = DiscoveryStore(root / "discovery.sqlite")
            with closing(store.connection()) as connection:
                connection.execute(
                    "INSERT INTO catalog_packs VALUES "
                    "('approved-pack', 'Cola 330ml Can', 'Cola', 'Original', 1, 330, 'can', 'Cola')"
                )
                connection.commit()
            store.set_cell_state("dunnes", "approved-pack", "pending")
            store.set_cell_state("dunnes", "rejected-pack", "pending")
            store.set_cell_state("tesco", "excluded-pack", "pending")

            reconcile_json_decisions(store.database, mapping_path, rejection_path)
            states = dict(store.connection().execute(
                "SELECT retailer || ':' || catalog_id, state FROM discovery_cells"
            ).fetchall())

        self.assertEqual(states, {
            "dunnes:approved-pack": "approved",
            "dunnes:rejected-pack": "rejected",
            "tesco:excluded-pack": "do_not_map",
        })

    def test_reconcile_does_not_resurrect_a_superseded_rejection(self):
        # A durable superseded row must stay superseded across reconciliation:
        # the replay never flips state back to a live rejection.
        rejections = {"listings": [{
            "canonical_key": "dunnes:sku-1:item-1",
            "retailer": "dunnes",
            "catalog_id": "pack-1",
            "rejected_at": "2025-01-01T00:00:00Z",
            "decided_by": "operator",
            "reason": "wrong pack",
            "state": "superseded",
            "superseded_at": "2025-01-02T00:00:00Z",
        }], "cells": []}
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            mapping_path = root / "mappings.json"
            rejection_path = root / "rejections.json"
            write_mappings(mapping_path, {})
            write_rejections(rejection_path, rejections)
            store = DiscoveryStore(root / "discovery.sqlite")
            store.upsert_candidate(
                "dunnes:sku-1:item-1", retailer="dunnes", identity_key="sku-1:item-1",
                identity_basis="product_reference:item_id", identity_tier="composite",
                source_product_reference="sku-1", source_item_id="item-1",
                source_product_name="Cola 330ml Can",
            )
            store.set_cell_state("dunnes", "pack-1", "pending")

            reconcile_json_decisions(store.database, mapping_path, rejection_path)
            first = store.connection().execute(
                "SELECT state FROM discovery_rejections "
                "WHERE canonical_key='dunnes:sku-1:item-1'"
            ).fetchall()
            cell = store.connection().execute(
                "SELECT state FROM discovery_cells "
                "WHERE retailer='dunnes' AND catalog_id='pack-1'"
            ).fetchone()
            status = store.connection().execute(
                "SELECT status FROM catalog_candidates WHERE candidate_id='dunnes:sku-1:item-1'"
            ).fetchone()

            reconcile_json_decisions(store.database, mapping_path, rejection_path)
            second = store.connection().execute(
                "SELECT state FROM discovery_rejections "
                "WHERE canonical_key='dunnes:sku-1:item-1'"
            ).fetchall()

        self.assertEqual(first, [("superseded",)])
        self.assertEqual(second, first)
        self.assertEqual(cell, ("pending",))
        self.assertEqual(status, ("pending_review",))

    def test_reconcile_same_second_rejections_for_one_identity_collapse_but_stay_keyed(self):
        # The PK (section, canonical_key, rejected_at) collapses two rejections
        # of the same candidate identity in the same second (INSERT OR IGNORE,
        # docs/discovery-and-review.md) — without error — while a different
        # candidate identity at the same second still gets its own row.
        rejections = {"listings": [
            {
                "canonical_key": "dunnes:sku-1:item-1",
                "retailer": "dunnes",
                "catalog_id": "pack-1",
                "rejected_at": "2025-01-01T00:00:00Z",
                "decided_by": "operator",
                "reason": "first copy",
                "state": "rejected",
            },
            {
                "canonical_key": "dunnes:sku-1:item-1",
                "retailer": "dunnes",
                "catalog_id": "pack-2",
                "rejected_at": "2025-01-01T00:00:00Z",
                "decided_by": "operator",
                "reason": "same-second duplicate for another cell",
                "state": "rejected",
            },
            {
                "canonical_key": "dunnes:sku-2:item-2",
                "retailer": "dunnes",
                "catalog_id": "pack-1",
                "rejected_at": "2025-01-01T00:00:00Z",
                "decided_by": "operator",
                "reason": "different identity, same second",
                "state": "rejected",
            },
        ], "cells": []}
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            mapping_path = root / "mappings.json"
            rejection_path = root / "rejections.json"
            write_mappings(mapping_path, {})
            write_rejections(rejection_path, rejections)
            store = DiscoveryStore(root / "discovery.sqlite")

            reconcile_json_decisions(store.database, mapping_path, rejection_path)

            rows = store.connection().execute(
                "SELECT canonical_key, catalog_id FROM discovery_rejections ORDER BY canonical_key"
            ).fetchall()

        self.assertEqual(rows, [
            ("dunnes:sku-1:item-1", "pack-1"),
            ("dunnes:sku-2:item-2", "pack-1"),
        ])

    def test_reconcile_operator_approved_mapping_outranks_search_provenance(self):
        # Operator approval outranks search provenance (docs/discovery-and-review.md):
        # an operator-decided approved mapping in SQLite must survive
        # reconciliation even when the JSON rejections disagree — the mapping
        # guard short-circuits the rejection's cell demotion.
        mappings = {"dunnes": [{
            "catalog_id": "pack-1",
            "expected_product_name": "Cola 330ml Can",
            "source_product_reference": "sku-1",
            "source_item_id": "item-1",
            "status": "approved",
            "decision_kind": "operator",
            "decided_by": "alice",
            "decided_at": "2025-01-01T00:00:00Z",
        }]}
        rejections = {"listings": [{
            "canonical_key": "dunnes:sku-2:item-2",
            "retailer": "dunnes",
            "catalog_id": "pack-1",
            "rejected_at": "2025-01-01T00:00:01Z",
            "decided_by": "agent-sprint",
            "reason": "search provenance claims a competing identity",
            "state": "rejected",
        }], "cells": []}
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            mapping_path = root / "mappings.json"
            rejection_path = root / "rejections.json"
            write_mappings(mapping_path, mappings)
            write_rejections(rejection_path, rejections)
            store = DiscoveryStore(root / "discovery.sqlite")
            with closing(store.connection()) as connection:
                connection.execute(
                    "INSERT INTO catalog_packs VALUES "
                    "('pack-1', 'Cola 330ml Can', 'Cola', 'Original', 1, 330, 'can', 'Cola')"
                )
                connection.commit()

            reconcile_json_decisions(store.database, mapping_path, rejection_path)

            with closing(store.connection()) as connection:
                mapping_state = connection.execute(
                    "SELECT status, decided_by FROM catalog_mappings "
                    "WHERE retailer='dunnes' AND catalog_id='pack-1'"
                ).fetchone()
                cell_state = connection.execute(
                    "SELECT state, decided_by FROM discovery_cells "
                    "WHERE retailer='dunnes' AND catalog_id='pack-1'"
                ).fetchone()

        self.assertEqual(mapping_state, ("approved", "alice"))
        self.assertEqual(cell_state, ("approved", "alice"))

    def test_reconciliation_repairs_sqlite_from_json_and_preserves_observation_tables(self):
        mappings = {"dunnes": [{
            "catalog_id": "pack-1",
            "expected_product_name": "Cola 330ml Can",
            "source_product_reference": "sku-1",
            "source_item_id": "item-1",
            "status": "approved",
        }]}
        rejections = {"listings": [], "cells": [{
            "retailer": "tesco",
            "catalog_id": "pack-2",
            "rejected_at": "2025-01-01T00:00:00Z",
            "decided_by": "operator",
            "reason": "not comparable",
            "state": "do_not_map",
        }]}
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            database = root / "feed.sqlite"
            mapping_path = root / "mappings.json"
            rejection_path = root / "rejections.json"
            write_mappings(mapping_path, mappings)
            write_rejections(rejection_path, rejections)
            with sqlite3.connect(database) as connection:
                connection.executescript("CREATE TABLE price_observations (id INTEGER)")
                connection.execute("CREATE TABLE catalog_packs (catalog_id TEXT PRIMARY KEY, name TEXT NOT NULL, brand TEXT NOT NULL, variant TEXT NOT NULL, pack_count INTEGER NOT NULL, unit_size_ml INTEGER NOT NULL, package_type TEXT NOT NULL, search_term TEXT NOT NULL)")
                connection.execute("INSERT INTO catalog_packs VALUES ('pack-1', 'Cola 330ml Can', 'Cola', 'Original', 1, 330, 'can', 'Cola')")
                connection.commit()

            reconcile_json_decisions(database, mapping_path, rejection_path)

            with sqlite3.connect(database) as connection:
                self.assertEqual(connection.execute("SELECT status, source_item_id FROM catalog_mappings").fetchone(), ("approved", "item-1"))
                self.assertEqual(connection.execute("SELECT state FROM discovery_cells WHERE retailer='tesco'").fetchone()[0], "do_not_map")
                self.assertEqual(connection.execute("SELECT COUNT(*) FROM price_observations").fetchone()[0], 0)


if __name__ == "__main__":
    unittest.main()
