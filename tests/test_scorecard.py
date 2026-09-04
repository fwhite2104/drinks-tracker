"""Tests for the catalog comparability scorecard (full-feed-coverage step 2).

All fixtures are hermetic: a temporary SQLite database with the three signal
tables and a temporary catalog.json. No live database, no retailer requests.
"""

from __future__ import annotations

import contextlib
import io
import json
import sqlite3
import tempfile
import unittest
from contextlib import closing
from pathlib import Path

from beverage_feed.scorecard import build_scorecard, main, proposed_catalog


def _write_signal_database(path: Path) -> None:
    with closing(sqlite3.connect(path)) as connection:
        connection.executescript(
            """
            CREATE TABLE discovery_candidate_cells (
                candidate_id TEXT PRIMARY KEY, retailer TEXT NOT NULL,
                catalog_id TEXT NOT NULL
            );
            CREATE TABLE catalog_mappings (
                catalog_id TEXT NOT NULL, retailer TEXT NOT NULL, status TEXT NOT NULL
            );
            CREATE TABLE discovery_rejections (
                section TEXT NOT NULL, canonical_key TEXT NOT NULL, retailer TEXT NOT NULL,
                catalog_id TEXT, state TEXT NOT NULL
            );
            """
        )
        rows = [
            # pack-a: two approved retailers + a third candidate retailer.
            ("INSERT INTO catalog_mappings VALUES ('pack-a', ?, 'approved')", ("dunnes",)),
            ("INSERT INTO catalog_mappings VALUES ('pack-a', ?, 'approved')", ("tesco",)),
            ("INSERT INTO discovery_candidate_cells VALUES ('c1', 'supervalu', 'pack-a')", ()),
            # pack-b: two candidate retailers, no approvals.
            ("INSERT INTO discovery_candidate_cells VALUES ('c2', 'dunnes', 'pack-b')", ()),
            ("INSERT INTO discovery_candidate_cells VALUES ('c3', 'lidl', 'pack-b')", ()),
            ("INSERT INTO discovery_candidate_cells VALUES ('c4', 'lidl', 'pack-b')", ()),
            # pack-c: single retailer, dominated by rejections.
            ("INSERT INTO discovery_candidate_cells VALUES ('c5', 'aldi', 'pack-c')", ()),
            ("INSERT INTO discovery_rejections VALUES "
             "('listings', 'k1', 'aldi', 'pack-c', 'rejected')", ()),
            ("INSERT INTO discovery_rejections VALUES "
             "('listings', 'k2', 'aldi', 'pack-c', 'rejected')", ()),
            # pack-d: approved but nothing else.
            ("INSERT INTO catalog_mappings VALUES ('pack-d', ?, 'approved')", ("dunnes",)),
            # pack-e: no signal at all.
        ]
        for statement, parameters in rows:
            connection.execute(statement, parameters)
        connection.commit()


CATALOG = [
    {"catalog_id": "pack-a", "name": "Cola A 330ml", "brand": "A", "variant": "v",
     "pack_count": 1, "unit_size_ml": 330, "package_type": "can",
     "search_term": "Cola A 330ml"},
    {"catalog_id": "pack-b", "name": "Cola B 500ml", "brand": "B", "variant": "v",
     "pack_count": 1, "unit_size_ml": 500, "package_type": "bottle",
     "search_term": "Cola B 500ml"},
    {"catalog_id": "pack-c", "name": "Cola C 1L", "brand": "C", "variant": "v",
     "pack_count": 1, "unit_size_ml": 1000, "package_type": "bottle",
     "search_term": "Cola C 1L"},
    {"catalog_id": "pack-d", "name": "Cola D 2L", "brand": "D", "variant": "v",
     "pack_count": 1, "unit_size_ml": 2000, "package_type": "bottle",
     "search_term": "Cola D 2L"},
    {"catalog_id": "pack-e", "name": "Cola E 330ml", "brand": "E", "variant": "v",
     "pack_count": 1, "unit_size_ml": 330, "package_type": "can",
     "search_term": "Cola E 330ml"},
]


class ScorecardTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.database = self.root / "feed.sqlite"
        _write_signal_database(self.database)
        self.catalog = self.root / "catalog.json"
        self.catalog.write_text(json.dumps(CATALOG))

    def test_scores_verdicts_from_cross_retailer_evidence(self):
        rows = {row["catalog_id"]: row for row in build_scorecard(self.catalog, self.database)}

        self.assertEqual(rows["pack-a"]["verdict"], "comparable")
        self.assertEqual(sorted(rows["pack-a"]["approved_retailers"]), ["dunnes", "tesco"])
        self.assertEqual(rows["pack-a"]["candidate_retailers"], ["supervalu"])
        self.assertEqual(rows["pack-b"]["verdict"], "comparable")
        self.assertEqual(rows["pack-b"]["candidates"], 3)
        self.assertEqual(rows["pack-c"]["verdict"], "single-retailer")
        self.assertEqual(rows["pack-c"]["rejections"], 2)
        self.assertEqual(rows["pack-d"]["verdict"], "single-retailer")
        self.assertEqual(rows["pack-e"]["verdict"], "no-signal")

    def test_ranks_by_approved_then_candidate_breadth(self):
        rows = build_scorecard(self.catalog, self.database)
        order = [row["catalog_id"] for row in rows]

        # pack-a (2 approved) first, then pack-d (1 approved — a confirmed
        # mapping outranks raw candidates), then pack-b (2 candidate
        # retailers, 3 candidates) before pack-c; pack-e last.
        self.assertEqual(order, ["pack-a", "pack-d", "pack-b", "pack-c", "pack-e"])
        self.assertEqual([row["rank"] for row in rows], [1, 2, 3, 4, 5])

    def test_proposed_catalog_keeps_comparable_and_drops_the_rest(self):
        rows = build_scorecard(self.catalog, self.database)
        keep, drop = proposed_catalog(rows)

        self.assertEqual([row["catalog_id"] for row in keep], ["pack-a", "pack-b"])
        self.assertEqual(
            [row["catalog_id"] for row in drop],
            ["pack-d", "pack-c", "pack-e"],
        )

    def test_main_writes_the_report_and_prints_a_summary(self):
        output = self.root / "scorecard.md"
        stdout = io.StringIO()

        with contextlib.redirect_stdout(stdout):
            code = main([
                "--catalog", str(self.catalog),
                "--database", str(self.database),
                "--output", str(output),
            ])

        self.assertEqual(code, 0)
        self.assertIn("comparable=2", stdout.getvalue())
        report = output.read_text()
        self.assertIn("# Catalog comparability scorecard", report)
        self.assertIn("pack-b", report)
        self.assertIn("proven comparable", report)
        self.assertIn("no retailer has ever returned a candidate", report)
