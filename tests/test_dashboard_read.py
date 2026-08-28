"""Hermetic tests for the read-only dashboard data seam."""

from __future__ import annotations

import json
import sqlite3
import tempfile
import unittest
from contextlib import closing
from pathlib import Path

from beverage_feed.collector import (
    AldiMapping,
    BenchmarkPack,
    ensure_schema,
    collect_aldi_one,
)
from beverage_feed.dashboard_read import (
    RETAILER_SLUGS,
    catalog_table,
    collection_health,
    coverage_matrix,
    discovery_summary,
    feed_preview,
    load_workspace,
    overview_stats,
    pack_detail,
    sprint_progress,
)
from beverage_feed.discovery import ensure_discovery_schema


PACK = BenchmarkPack(
    catalog_id="water-5l",
    name="Comeragh Still Water 5L Bottle",
    brand="Comeragh",
    variant="Still Water",
    pack_count=1,
    unit_size_ml=5000,
    package_type="bottle",
    search_term="Still Water",
)

MULTIPACK = BenchmarkPack(
    catalog_id="coke-330-8",
    name="Coca-Cola Original 8x330ml",
    brand="Coca-Cola",
    variant="Original",
    pack_count=8,
    unit_size_ml=330,
    package_type="can",
    search_term="Coca-Cola 8 pack",
)


def _write_json(path: Path, value: object) -> None:
    path.write_text(json.dumps(value, indent=2) + "\n")


def _workspace_tree(
    root: Path,
    *,
    catalog: list[dict] | None = None,
    mappings: dict | None = None,
    rejections: dict | None = None,
    database: Path | None = None,
) -> Path:
    data = root / "data"
    data.mkdir(parents=True, exist_ok=True)
    _write_json(
        data / "catalog.json",
        catalog
        if catalog is not None
        else [
            {
                "catalog_id": PACK.catalog_id,
                "name": PACK.name,
                "brand": PACK.brand,
                "variant": PACK.variant,
                "pack_count": PACK.pack_count,
                "unit_size_ml": PACK.unit_size_ml,
                "package_type": PACK.package_type,
                "search_term": PACK.search_term,
            },
            {
                "catalog_id": MULTIPACK.catalog_id,
                "name": MULTIPACK.name,
                "brand": MULTIPACK.brand,
                "variant": MULTIPACK.variant,
                "pack_count": MULTIPACK.pack_count,
                "unit_size_ml": MULTIPACK.unit_size_ml,
                "package_type": MULTIPACK.package_type,
                "search_term": MULTIPACK.search_term,
            },
        ],
    )
    _write_json(
        data / "mappings.json",
        mappings
        if mappings is not None
        else {
            "aldi": [
                {
                    "catalog_id": PACK.catalog_id,
                    "expected_product_name": "Still Water",
                    "status": "approved",
                }
            ],
            "tesco": [
                {
                    "catalog_id": MULTIPACK.catalog_id,
                    "expected_product_name": "Coca-Cola 8 pack",
                    "status": "approved",
                    "source_tpnb": "123",
                }
            ],
        },
    )
    _write_json(
        data / "rejections.json",
        rejections
        if rejections is not None
        else {"listings": [], "cells": []},
    )
    if database is not None:
        # Caller manages the sqlite file path; default name is feed.sqlite.
        pass
    return root


class NoDatabaseTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        _workspace_tree(self.root)

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def test_load_workspace_reports_no_database_without_creating_file(self) -> None:
        snapshot = load_workspace(self.root)
        db_path = self.root / "data" / "feed.sqlite"

        self.assertFalse(db_path.exists())
        self.assertEqual(snapshot.workspace_state, "no_database")
        self.assertFalse(snapshot.database.exists)
        self.assertFalse(snapshot.database.openable)
        self.assertEqual(len(snapshot.catalog), 2)
        self.assertFalse(db_path.exists())  # still not created

    def test_overview_stats_zero_observations_when_no_database(self) -> None:
        stats = overview_stats(load_workspace(self.root))

        self.assertEqual(stats["catalog_packs"], 2)
        self.assertEqual(stats["approved_mappings"], 2)
        self.assertEqual(stats["supported_retailers"], len(RETAILER_SLUGS))
        self.assertEqual(stats["observation_count"], 0)
        self.assertEqual(stats["workspace_state"], "no_database")

    def test_collection_health_not_collected_for_every_retailer(self) -> None:
        rows = collection_health(load_workspace(self.root))

        self.assertEqual(len(rows), len(RETAILER_SLUGS))
        self.assertTrue(all(row["state"] == "not_collected" for row in rows))
        self.assertTrue(all(row["label"] == "Not collected" for row in rows))

    def test_discovery_summary_no_run_yet(self) -> None:
        summary = discovery_summary(load_workspace(self.root))

        self.assertEqual(summary["state"], "no_discovery_run")
        self.assertEqual(summary["label"], "No discovery run yet")

    def test_coverage_matrix_uses_json_approved_mappings(self) -> None:
        matrix = coverage_matrix(load_workspace(self.root))

        by_id = {pack["catalog_id"]: pack for pack in matrix["packs"]}
        self.assertTrue(by_id[PACK.catalog_id]["cells"]["aldi"]["approved"])
        self.assertFalse(by_id[PACK.catalog_id]["cells"]["tesco"]["approved"])
        self.assertEqual(
            by_id[PACK.catalog_id]["cells"]["tesco"]["mapping_state"], "unmapped"
        )
        self.assertEqual(matrix["approved_mappings"], 2)

    def test_feed_preview_awaiting_price_and_not_available(self) -> None:
        preview = feed_preview(load_workspace(self.root))

        self.assertIn("stock", preview["standing_rule"].lower())
        by_id = {pack["catalog_id"]: pack for pack in preview["packs"]}
        self.assertIn(PACK.catalog_id, by_id)
        cells = {cell["retailer"]: cell for cell in by_id[PACK.catalog_id]["retailers"]}
        self.assertEqual(cells["aldi"]["state"], "awaiting_price")
        self.assertEqual(cells["aldi"]["label"], "Awaiting price")
        self.assertEqual(cells["tesco"]["state"], "not_available")
        self.assertEqual(cells["tesco"]["label"], "Not available")
        # Pack with zero approved mappings would be omitted; multipack has tesco.
        self.assertIn(MULTIPACK.catalog_id, by_id)

    def test_feed_preview_omits_packs_with_zero_approved_mappings(self) -> None:
        _workspace_tree(
            self.root,
            mappings={"aldi": []},  # empty — no approved
        )
        # rewrite mappings to empty object of supported shape
        _write_json(self.root / "data" / "mappings.json", {"aldi": []})
        preview = feed_preview(load_workspace(self.root))
        self.assertEqual(preview["packs"], [])

    def test_do_not_map_cell_is_not_available_on_preview(self) -> None:
        _write_json(
            self.root / "data" / "rejections.json",
            {
                "listings": [],
                "cells": [
                    {
                        "retailer": "aldi",
                        "catalog_id": PACK.catalog_id,
                        "cell": PACK.catalog_id,
                        "rejected_at": "2024-01-01T00:00:00Z",
                        "decided_by": "operator",
                        "state": "do_not_map",
                    }
                ],
            },
        )
        # still has tesco multipack mapping so workspace loads; PACK aldi approved
        # but do_not_map should win over approved only when no approved... 
        # Spec: admin mapping state do_not_map when in rejected cells.
        # If also approved in JSON, approved wins (JSON approved is authoritative).
        # Clear aldi mapping and rely on rejection alone.
        _write_json(
            self.root / "data" / "mappings.json",
            {
                "tesco": [
                    {
                        "catalog_id": MULTIPACK.catalog_id,
                        "expected_product_name": "Coca-Cola 8 pack",
                        "status": "approved",
                        "source_tpnb": "123",
                    }
                ]
            },
        )
        snapshot = load_workspace(self.root)
        matrix = coverage_matrix(snapshot)
        pack = next(p for p in matrix["packs"] if p["catalog_id"] == PACK.catalog_id)
        self.assertEqual(pack["cells"]["aldi"]["mapping_state"], "do_not_map")


class WithObservationsTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        _workspace_tree(self.root)
        self.database = self.root / "data" / "feed.sqlite"
        record = {
            "productId": "000000000000336021",
            "name": "Still Water",
            "brand": "COMERAGH",
            "price": "€1.45",
        }
        collect_aldi_one(
            PACK,
            AldiMapping(catalog_id=PACK.catalog_id, expected_product_name="Still Water"),
            lambda _: {"items": [record]},
            self.database,
        )

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def test_workspace_partial_run_and_observation_count(self) -> None:
        snapshot = load_workspace(self.root)
        self.assertEqual(snapshot.workspace_state, "partial_run")
        self.assertTrue(snapshot.database.openable)
        stats = overview_stats(snapshot)
        self.assertEqual(stats["observation_count"], 1)

    def test_collection_health_marks_aldi_collected(self) -> None:
        rows = {row["retailer"]: row for row in collection_health(load_workspace(self.root))}
        self.assertEqual(rows["aldi"]["state"], "collected")
        self.assertGreaterEqual(rows["aldi"]["observed"], 1)
        self.assertEqual(rows["tesco"]["state"], "not_collected")

    def test_feed_preview_shows_displayed_price_for_observed_cell(self) -> None:
        preview = feed_preview(load_workspace(self.root))
        pack = next(p for p in preview["packs"] if p["catalog_id"] == PACK.catalog_id)
        aldi = next(c for c in pack["retailers"] if c["retailer"] == "aldi")
        self.assertEqual(aldi["state"], "observed")
        self.assertEqual(aldi["displayed_price"], "1.45")
        self.assertTrue(aldi["is_best"])

    def test_pack_detail_reports_current_observation(self) -> None:
        detail = pack_detail(load_workspace(self.root), PACK.catalog_id)
        assert detail is not None
        aldi = next(r for r in detail["retailers"] if r["retailer"] == "aldi")
        self.assertEqual(aldi["mapping_state"], "approved")
        self.assertEqual(aldi["collection_state"], "observed")
        self.assertEqual(aldi["observation_state"], "current")
        self.assertEqual(aldi["current_observation"]["displayed_price"], "1.45")

    def test_read_path_does_not_write_to_database(self) -> None:
        before = self.database.stat().st_mtime_ns
        snapshot = load_workspace(self.root)
        overview_stats(snapshot)
        collection_health(snapshot)
        feed_preview(snapshot)
        pack_detail(snapshot, PACK.catalog_id)
        coverage_matrix(snapshot)
        catalog_table(snapshot)
        after = self.database.stat().st_mtime_ns
        self.assertEqual(before, after)


class SourceErrorAndLastSeenTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        _workspace_tree(self.root)
        self.database = self.root / "data" / "feed.sqlite"

        # First: successful observation via collect helper.
        collect_aldi_one(
            PACK,
            AldiMapping(catalog_id=PACK.catalog_id, expected_product_name="Still Water"),
            lambda _: {
                "items": [
                    {
                        "productId": "000000000000336021",
                        "name": "Still Water",
                        "brand": "COMERAGH",
                        "price": "€1.45",
                    }
                ]
            },
            self.database,
        )

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def _insert_result(
        self,
        *,
        run_id: str,
        status: str,
        recorded_at: str,
        error: str | None = None,
    ) -> None:
        with closing(sqlite3.connect(self.database)) as connection:
            ensure_schema(connection)
            connection.execute(
                """
                INSERT OR REPLACE INTO collection_runs
                    (run_id, started_at, finished_at, status, observed_count,
                     failed_count, summary)
                VALUES (?, ?, ?, ?, 0, 1, ?)
                """,
                (run_id, recorded_at, recorded_at, "completed", "{}"),
            )
            connection.execute(
                """
                INSERT INTO collection_results
                    (run_id, catalog_id, retailer, status, error, source_product_reference,
                     source_item_id, source_scope, recorded_at)
                VALUES (?, ?, 'aldi', ?, ?, NULL, NULL, NULL, ?)
                """,
                (run_id, PACK.catalog_id, status, error, recorded_at),
            )
            connection.commit()

    def test_source_error_maps_to_temporarily_unavailable_on_preview(self) -> None:
        self._insert_result(
            run_id="run-err",
            status="source_error",
            recorded_at="2099-01-02T00:00:00Z",
            error="timeout",
        )
        preview = feed_preview(load_workspace(self.root))
        pack = next(p for p in preview["packs"] if p["catalog_id"] == PACK.catalog_id)
        aldi = next(c for c in pack["retailers"] if c["retailer"] == "aldi")
        self.assertEqual(aldi["state"], "temporarily_unavailable")
        self.assertEqual(aldi["label"], "Temporarily unavailable")
        self.assertIsNone(aldi["displayed_price"])

    def test_not_found_after_observation_uses_last_seen_not_old_price(self) -> None:
        self._insert_result(
            run_id="run-nf",
            status="not_found",
            recorded_at="2099-01-03T00:00:00Z",
        )
        preview = feed_preview(load_workspace(self.root))
        pack = next(p for p in preview["packs"] if p["catalog_id"] == PACK.catalog_id)
        aldi = next(c for c in pack["retailers"] if c["retailer"] == "aldi")
        self.assertEqual(aldi["state"], "last_seen")
        self.assertEqual(aldi["label"], "Last seen")
        self.assertIsNone(aldi["displayed_price"])
        self.assertIsNotNone(aldi.get("last_seen_at"))

    def test_empty_database_file_is_no_run(self) -> None:
        # Fresh schema, no runs.
        empty = self.root / "data" / "empty.sqlite"
        with closing(sqlite3.connect(empty)) as connection:
            ensure_schema(connection)
            connection.commit()
        snapshot = load_workspace(self.root, database_path=empty)
        self.assertEqual(snapshot.workspace_state, "no_run")
        self.assertEqual(overview_stats(snapshot)["observation_count"], 0)


class DiscoveryStateTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        _workspace_tree(self.root)
        self.database = self.root / "data" / "feed.sqlite"
        ensure_discovery_schema(self.database)
        with closing(sqlite3.connect(self.database)) as connection:
            connection.execute(
                "INSERT INTO discovery_runs(run_id, started_at, status) "
                "VALUES ('d1', '2024-01-01T00:00:00Z', 'completed')"
            )
            connection.execute(
                """
                INSERT INTO discovery_cells
                    (retailer, catalog_id, state, review_category, candidate_id,
                     decided_at, decided_by, reason)
                VALUES ('aldi', ?, 'review', 'missing', NULL, '2024-01-01T00:00:00Z',
                        'discovery', NULL)
                """,
                (PACK.catalog_id,),
            )
            connection.execute(
                """
                INSERT INTO discovery_cells
                    (retailer, catalog_id, state, review_category, candidate_id,
                     decided_at, decided_by, reason)
                VALUES ('tesco', ?, 'review', 'challenge', NULL, '2024-01-01T00:00:00Z',
                        'discovery', NULL)
                """,
                (MULTIPACK.catalog_id,),
            )
            connection.commit()

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def test_discovery_summary_available_with_per_retailer_counts(self) -> None:
        summary = discovery_summary(load_workspace(self.root))
        self.assertEqual(summary["state"], "available")
        self.assertGreaterEqual(summary["discovery_runs"], 1)
        by_retailer = {row["retailer"]: row for row in summary["per_retailer"]}
        self.assertGreaterEqual(by_retailer["aldi"]["review"], 1)
        self.assertGreaterEqual(by_retailer["tesco"]["challenge"], 1)

    def test_coverage_matrix_marks_challenge_and_pending(self) -> None:
        matrix = coverage_matrix(load_workspace(self.root))
        by_id = {pack["catalog_id"]: pack for pack in matrix["packs"]}
        # PACK has approved aldi mapping — approved wins over discovery review.
        self.assertEqual(
            by_id[PACK.catalog_id]["cells"]["aldi"]["mapping_state"], "approved"
        )
        # MULTIPACK has approved tesco mapping; challenge still surfaces.
        self.assertEqual(
            by_id[MULTIPACK.catalog_id]["cells"]["tesco"]["mapping_state"],
            "challenge",
        )


class MultipackComponentPriceTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        _workspace_tree(
            self.root,
            mappings={
                "tesco": [
                    {
                        "catalog_id": MULTIPACK.catalog_id,
                        "expected_product_name": "Coca-Cola 8 pack",
                        "status": "approved",
                        "source_tpnb": "123",
                    }
                ]
            },
        )
        self.database = self.root / "data" / "feed.sqlite"
        with closing(sqlite3.connect(self.database)) as connection:
            ensure_schema(connection)
            connection.execute(
                """
                INSERT INTO collection_runs
                    (run_id, started_at, finished_at, status, observed_count,
                     failed_count, summary)
                VALUES ('r1', '2024-06-01T00:00:00Z', '2024-06-01T00:01:00Z',
                        'completed', 1, 0, '{}')
                """
            )
            connection.execute(
                """
                INSERT INTO collection_results
                    (run_id, catalog_id, retailer, status, error,
                     source_product_reference, source_item_id, source_scope, recorded_at)
                VALUES ('r1', ?, 'tesco', 'observed', NULL, 'tpnb', '1', NULL,
                        '2024-06-01T00:01:00Z')
                """,
                (MULTIPACK.catalog_id,),
            )
            connection.execute(
                """
                INSERT INTO price_observations (
                    run_id, catalog_id, retailer, source_product_reference,
                    source_item_id, source_product_name, displayed_price,
                    clubcard_price, drs_deposit, source_scope, currency,
                    pack_count, unit_size_ml, package_type, component_unit_price,
                    price_per_litre, observed_at
                ) VALUES (
                    'r1', ?, 'tesco', 'tpnb', '1', 'Coca-Cola 8', '4.00',
                    '3.50', '0.15', 'store-1', 'EUR', 8, 330, 'can', '0.50',
                    '1.5152', '2024-06-01T00:01:00Z'
                )
                """,
                (MULTIPACK.catalog_id,),
            )
            connection.commit()

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def test_feed_preview_exposes_clubcard_drs_and_component_unit_price(self) -> None:
        preview = feed_preview(load_workspace(self.root))
        pack = next(
            p for p in preview["packs"] if p["catalog_id"] == MULTIPACK.catalog_id
        )
        tesco = next(c for c in pack["retailers"] if c["retailer"] == "tesco")
        self.assertEqual(tesco["state"], "observed")
        self.assertEqual(tesco["displayed_price"], "4.00")
        self.assertEqual(tesco["clubcard_price"], "3.50")
        self.assertEqual(tesco["drs_deposit"], "0.15")
        self.assertEqual(tesco["component_unit_price"], "0.50")
        self.assertEqual(tesco["source_scope"], "store-1")
        # Component is secondary; ranking still on displayed price.
        self.assertTrue(tesco["is_best"])


class UnreadableDatabaseTests(unittest.TestCase):
    def test_corrupt_database_still_serves_json_identity(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            _workspace_tree(root)
            db = root / "data" / "feed.sqlite"
            db.write_text("this is not sqlite")
            snapshot = load_workspace(root)
            self.assertTrue(snapshot.database.exists)
            self.assertFalse(snapshot.database.openable)
            self.assertEqual(snapshot.workspace_state, "no_database")
            stats = overview_stats(snapshot)
            self.assertEqual(stats["catalog_packs"], 2)
            self.assertEqual(stats["observation_count"], 0)
            preview = feed_preview(snapshot)
            pack = next(p for p in preview["packs"] if p["catalog_id"] == PACK.catalog_id)
            aldi = next(c for c in pack["retailers"] if c["retailer"] == "aldi")
            self.assertEqual(aldi["state"], "awaiting_price")


class SprintProgressTests(unittest.TestCase):
    """Ticket 05: sprint progress buckets over the catalog × retailer bar."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        _workspace_tree(self.root)

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def test_buckets_sum_to_total_cells_and_classify_states(self) -> None:
        rejections = {
            "listings": [],
            "cells": [
                {
                    "retailer": "dunnes",
                    "catalog_id": PACK.catalog_id,
                    "cell": PACK.catalog_id,
                    "rejected_at": "2026-01-01T00:00:00Z",
                    "decided_by": "test",
                    "reason": "never stocked",
                    "state": "do_not_map",
                }
            ],
        }
        _workspace_tree(self.root, rejections=rejections)
        snapshot = load_workspace(self.root)
        progress = sprint_progress(snapshot)
        buckets = progress["buckets"]
        # 2 packs × 5 retailers: 2 approved (no observations yet → mapped),
        # 1 excluded, 7 untouched.
        self.assertEqual(progress["total_cells"], 10)
        self.assertEqual(
            sum(buckets.values()), progress["total_cells"]
        )
        self.assertEqual(buckets["observed"], 0)
        self.assertEqual(buckets["mapped_not_observed"], 2)
        self.assertEqual(buckets["excluded"], 1)
        self.assertEqual(buckets["untouched"], 7)
        dunnes = next(
            r for r in progress["per_retailer"] if r["retailer"] == "dunnes"
        )
        self.assertEqual(dunnes["excluded"], 1)

    def test_empty_workspace_is_all_untouched(self) -> None:
        _workspace_tree(self.root, mappings={}, rejections={"listings": [], "cells": []})
        snapshot = load_workspace(self.root)
        progress = sprint_progress(snapshot)
        self.assertEqual(progress["buckets"]["untouched"], 10)
        self.assertEqual(progress["buckets"]["mapped_not_observed"], 0)


if __name__ == "__main__":
    unittest.main()
