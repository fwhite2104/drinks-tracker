import json
import sqlite3
import tempfile
import unittest
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
