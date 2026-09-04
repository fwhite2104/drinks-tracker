"""Tests for the surgical discovery-database merge (discovery_merge).

Hermetic: two temp fixture DBs sharing the real discovery schema, no live
retailer calls.  Pins the three behaviours the VM merge relies on: only
missing rows are copied, existing target decisions are never overwritten,
and collection tables are untouched.
"""

from __future__ import annotations

import sqlite3
import tempfile
import unittest
from contextlib import closing
from pathlib import Path

from beverage_feed.discovery import ensure_discovery_schema
from beverage_feed.discovery_merge import merge_discovery_database


def _seed_run(connection: sqlite3.Connection, run_id: str, status: str = "complete") -> None:
    connection.execute(
        "INSERT INTO discovery_runs(run_id, started_at, status) VALUES (?, ?, ?)",
        (run_id, "2026-09-04T19:30:44Z", status),
    )


class DiscoveryMergeTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.source = Path(self._tmp.name) / "source.sqlite"
        self.target = Path(self._tmp.name) / "target.sqlite"
        for path in (self.source, self.target):
            ensure_discovery_schema(path)
        with closing(sqlite3.connect(self.source)) as con, closing(sqlite3.connect(self.target)) as dst:
            # Shared history: one run already present in both DBs.
            _seed_run(con, "run-old")
            _seed_run(dst, "run-old")
            # Target keeps a fresher local decision the source lacks.
            dst.execute(
                "INSERT INTO discovery_cells(retailer, catalog_id, state, decided_by) "
                "VALUES ('supervalu', 'local-pack', 'approved', 'human')"
            )
            dst.execute(
                "INSERT INTO price_observations(run_id, catalog_id, retailer, source_product_reference, "
                "source_item_id, source_product_name, displayed_price, currency, pack_count, "
                "unit_size_ml, package_type, observed_at) "
                "VALUES ('run-obs', 'local-pack', 'supervalu', 'ref-1', 'item-1', 'Coke', '3.49', "
                "'EUR', 1, 330, 'can', '2026-09-04T06:00:00Z')"
            )
            dst.commit()
            # Source adds the CI first-discovery results.
            _seed_run(con, "run-ci")
            con.execute(
                "INSERT INTO discovery_attempts(attempt_id, run_id, started_at, status) "
                "VALUES ('att-ci', 'run-ci', '2026-09-04T19:30:44Z', 'complete')"
            )
            con.execute(
                "INSERT INTO discovery_cells(retailer, catalog_id, state, candidate_id, decided_by, reason) "
                "VALUES ('supervalu', 'new-pack', 'unmapped', 'cand-1', 'agent-sprint', 'first-discovery')"
            )
            # Source also carries a stale copy of the target's approved cell —
            # must NOT overwrite the target decision.
            con.execute(
                "INSERT INTO discovery_cells(retailer, catalog_id, state, decided_by) "
                "VALUES ('supervalu', 'local-pack', 'pending', 'ci')"
            )
            con.execute(
                "INSERT INTO discovery_candidate_cells(candidate_id, retailer, catalog_id, first_seen_at, last_seen_at) "
                "VALUES ('cand-1', 'supervalu', 'new-pack', '2026-09-04T19:31:00Z', '2026-09-04T19:31:00Z')"
            )
            con.execute(
                "INSERT INTO discovery_candidate_search_terms(candidate_id, retailer, search_term, first_seen_at, last_seen_at) "
                "VALUES ('cand-1', 'supervalu', 'coca cola 330ml', '2026-09-04T19:31:00Z', '2026-09-04T19:31:00Z')"
            )
            con.execute(
                "INSERT INTO discovery_candidate_evidence(candidate_id, retailer, catalog_id, recorded_at, raw_price_value) "
                "VALUES ('cand-1', 'supervalu', 'new-pack', '2026-09-04T19:31:00Z', '3.29')"
            )
            con.execute(
                "INSERT INTO discovery_search_history(run_id, attempt_id, retailer, catalog_id, search_term, searched_at, complete) "
                "VALUES ('run-ci', 'att-ci', 'supervalu', 'new-pack', 'coca cola 330ml', '2026-09-04T19:31:00Z', 'True')"
            )
            con.execute(
                "INSERT INTO discovery_state_transitions(retailer, catalog_id, from_state, to_state, changed_at, changed_by) "
                "VALUES ('supervalu', 'new-pack', 'pending', 'unmapped', '2026-09-04T19:31:00Z', 'agent-sprint')"
            )
            con.execute(
                "INSERT INTO discovery_rejections(section, canonical_key, retailer, rejected_at, decided_by, state) "
                "VALUES ('junk', 'k-1', 'supervalu', '2026-09-04T19:31:00Z', 'agent-sprint', 'rejected')"
            )
            con.commit()

    def _count(self, path: Path, table: str, where: str = "") -> int:
        with closing(sqlite3.connect(path)) as con:
            return con.execute(f"SELECT count(*) FROM {table} {where}").fetchone()[0]  # noqa: S608

    def test_merge_copies_only_missing_rows_by_natural_key(self) -> None:
        counts = merge_discovery_database(self.source, self.target)
        self.assertEqual(counts["discovery_runs"], 1)  # run-ci; run-old deduped
        self.assertEqual(counts["discovery_attempts"], 1)
        self.assertEqual(counts["discovery_cells"], 1)  # new-pack; local-pack deduped
        self.assertEqual(counts["discovery_candidate_cells"], 1)
        self.assertEqual(counts["discovery_candidate_search_terms"], 1)
        self.assertEqual(counts["discovery_candidate_evidence"], 1)
        self.assertEqual(counts["discovery_search_history"], 1)
        self.assertEqual(counts["discovery_state_transitions"], 1)
        self.assertEqual(counts["discovery_rejections"], 1)
        self.assertEqual(
            self._count(self.target, "discovery_cells", "WHERE retailer='supervalu'"), 2
        )

    def test_merge_reassigns_autoincrement_ids(self) -> None:
        merge_discovery_database(self.source, self.target)
        with closing(sqlite3.connect(self.target)) as con:
            ids = [row[0] for row in con.execute("SELECT search_id FROM discovery_search_history")]
        self.assertEqual(ids, [1])  # target-assigned id, not the source's

    def test_merge_never_overwrites_existing_target_decisions(self) -> None:
        merge_discovery_database(self.source, self.target)
        with closing(sqlite3.connect(self.target)) as con:
            state, decided_by = con.execute(
                "SELECT state, decided_by FROM discovery_cells "
                "WHERE retailer='supervalu' AND catalog_id='local-pack'"
            ).fetchone()
        self.assertEqual((state, decided_by), ("approved", "human"))

    def test_merge_is_idempotent(self) -> None:
        merge_discovery_database(self.source, self.target)
        second = merge_discovery_database(self.source, self.target)
        self.assertEqual(max(second.values()), 0)
        self.assertEqual(sum(second.values()), 0)

    def test_merge_never_touches_collection_tables(self) -> None:
        before = self._count(self.target, "price_observations")
        merge_discovery_database(self.source, self.target)
        self.assertEqual(self._count(self.target, "price_observations"), before)
        self.assertEqual(before, 1)

    def test_merge_rejects_missing_source(self) -> None:
        with self.assertRaises(ValueError):
            merge_discovery_database(self.target.parent / "nope.sqlite", self.target)

    def test_cli_reports_zero_when_nothing_new(self) -> None:
        from beverage_feed.discovery_merge import main

        self.assertEqual(main(["--source", str(self.source), "--target", str(self.target)]), 0)
        self.assertEqual(main(["--source", str(self.source), "--target", str(self.target)]), 0)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
