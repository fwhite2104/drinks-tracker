"""Tests for the passive freshness snapshot (beverage_feed/freshness.py)."""

import contextlib
import io
import sqlite3
import tempfile
import unittest
from contextlib import closing
from datetime import datetime, timedelta, timezone
from pathlib import Path

from beverage_feed.collector import _iso, ensure_schema
from beverage_feed.freshness import freshness_snapshot, main


class FreshnessTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.database = str(Path(self._tmp.name) / "feed.sqlite")
        with closing(sqlite3.connect(self.database)) as connection:
            ensure_schema(connection)
            self.now = datetime.now(timezone.utc)
            connection.execute(
                "INSERT INTO catalog_packs VALUES"
                " ('coca-original-500', 'Coke 500ml', 'Coca-Cola', 'original',"
                "  1, 500, 'bottle', 'coca cola 500')"
            )
            connection.executemany(
                "INSERT INTO catalog_mappings (catalog_id, retailer,"
                " expected_product_name, status) VALUES (?, ?, ?, 'approved')",
                [
                    ("coca-original-500", "dunnes", "Coke 500ml"),
                    ("coca-original-500", "supervalu", "Coke 500ml"),
                ],
            )
            connection.execute(
                "INSERT INTO collection_runs VALUES"
                " ('run-1', '2026-01-01T00:00:00Z', '2026-01-01T00:01:00Z',"
                "  'completed', 1, 0, '{}')"
            )
            connection.execute(
                "INSERT INTO price_observations (run_id, catalog_id, retailer,"
                " source_product_reference, source_item_id, source_product_name,"
                " displayed_price, currency, pack_count, unit_size_ml,"
                " package_type, observed_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    "run-1",
                    "coca-original-500",
                    "dunnes",
                    "1",
                    "1",
                    "Coke 500ml",
                    "2.00",
                    "EUR",
                    1,
                    500,
                    "bottle",
                    # Use the production timestamp format the collector writes,
                    # so as_datetime is verified against real persisted values.
                    _iso(self.now - timedelta(days=2)),
                ),
            )
            connection.commit()

    def test_reports_age_for_observed_and_none_for_frozen_retailer(self) -> None:
        rows = {row["retailer"]: row for row in freshness_snapshot(self.database)}
        self.assertIsNotNone(rows["dunnes"]["age_days"])
        self.assertAlmostEqual(rows["dunnes"]["age_days"], 2.0, delta=0.1)
        # Frozen-collection signature: mapped but never observed.
        self.assertIsNone(rows["supervalu"]["age_days"])
        self.assertIsNone(rows["supervalu"]["freshest_observation"])

    def test_freshest_observation_timestamp_is_reported_verbatim(self) -> None:
        rows = {row["retailer"]: row for row in freshness_snapshot(self.database)}
        self.assertEqual(
            rows["dunnes"]["freshest_observation"],
            _iso(self.now - timedelta(days=2)),
        )

    def test_cli_prints_never_observed_and_always_exits_zero(self) -> None:
        stdout = io.StringIO()
        with contextlib.redirect_stdout(stdout):
            code = main(["--database", self.database])

        output = stdout.getvalue()
        self.assertEqual(code, 0)
        self.assertIn("retailers=2", output)
        self.assertIn("never_observed=supervalu", output)

    def test_cli_with_no_observations_reports_never_observed_and_exits_zero(self) -> None:
        with closing(sqlite3.connect(self.database)) as connection:
            connection.execute("DELETE FROM price_observations")
            connection.commit()

        stdout = io.StringIO()
        with contextlib.redirect_stdout(stdout):
            code = main(["--database", self.database])

        output = stdout.getvalue()
        self.assertEqual(code, 0)
        self.assertIn("no observations", output)
        self.assertIn("never_observed=dunnes,supervalu", output)


if __name__ == "__main__":
    unittest.main()
