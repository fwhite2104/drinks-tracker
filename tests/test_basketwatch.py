"""BasketWatch external-source ingest coverage (mocked client, temp SQLite)."""

import io
import json
import sqlite3
import tempfile
import unittest
from contextlib import closing
from pathlib import Path
from unittest.mock import patch

from beverage_feed.basketwatch import BasketWatchClient, ingest_basketwatch_snapshot
from beverage_feed.collector import BenchmarkPack, ensure_schema

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


def _seed_database(database: Path) -> None:
    with closing(sqlite3.connect(database)) as connection:
        ensure_schema(connection)
        connection.execute(
            "INSERT INTO catalog_packs VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (PACK.catalog_id, PACK.name, PACK.brand, PACK.variant, PACK.pack_count,
             PACK.unit_size_ml, PACK.package_type, PACK.search_term),
        )
        connection.execute(
            """
            INSERT INTO catalog_mappings
                (catalog_id, retailer, expected_product_name, source_product_reference,
                 source_item_id, status)
            VALUES (?, 'tesco', ?, NULL, NULL, 'approved')
            """,
            (PACK.catalog_id, PACK.name),
        )
        connection.commit()


MATCHING_RECORD = {
    "productId": "bw-1",
    "name": "Comeragh Still Water 5L Bottle",
    "brand": "Comeragh",
    "variant": "Still Water",
    "price": "\u20ac1.45",
}

UNMATCHED_RECORD = {
    "productId": "bw-2",
    "name": "Mystery Fizzy Drink 330ml Can",
    "price": "\u20ac0.99",
}


class FakeClient:
    def __init__(self, records):
        self.records = records
        self.calls = []

    def fetch_all(self, retailer_slug):
        self.calls.append(retailer_slug)
        return self.records


class BasketWatchIngestTests(unittest.TestCase):
    def test_client_requires_api_key(self):
        with self.assertRaises(ValueError):
            BasketWatchClient("  ")

    def test_client_sends_bearer_auth_and_parses_records(self):
        requests = []

        class Response(io.BytesIO):
            status = 200

            def __enter__(self):
                return self

            def __exit__(self, *args):
                self.close()

        class Opener:
            def open(self, request, timeout):
                requests.append(request)
                return Response(json.dumps({"records": [MATCHING_RECORD]}).encode())

        client = BasketWatchClient("secret-key", endpoint="https://bw.test/v1", opener=Opener())
        records = client.fetch_all("tesco")

        self.assertEqual(records, [MATCHING_RECORD])
        self.assertIn("https://bw.test/v1?retailer=tesco", requests[0].full_url)
        self.assertEqual(requests[0].get_header("Authorization"), "Bearer secret-key")

    def test_ingest_without_api_key_raises_helpful_error(self):
        with tempfile.TemporaryDirectory() as directory:
            with self.assertRaises(EnvironmentError) as context:
                ingest_basketwatch_snapshot("tesco", Path(directory) / "db.sqlite", "")
        self.assertIn("BASKETWATCH_API_KEY", str(context.exception))

    def test_ingest_observes_approved_matches_and_queues_unmatched(self):
        with tempfile.TemporaryDirectory() as directory:
            database = Path(directory) / "feed.sqlite"
            _seed_database(database)

            summary = ingest_basketwatch_snapshot(
                "tesco", database, "test-key",
                client_factory=lambda key: FakeClient([MATCHING_RECORD, UNMATCHED_RECORD]),
            )

            self.assertEqual(summary["fetched"], 2)
            self.assertEqual(summary["ingested"], 1)
            self.assertEqual(summary["queued_candidates"], 1)
            with closing(sqlite3.connect(database)) as connection:
                observation = connection.execute(
                    """
                    SELECT retailer, catalog_id, displayed_price, clubcard_price,
                           drs_deposit, source_scope, currency, price_per_litre
                    FROM price_observations
                    """
                ).fetchone()
                result = connection.execute(
                    "SELECT status, source_scope FROM collection_results"
                ).fetchone()
                candidate = connection.execute(
                    "SELECT candidate_id, status, raw_record FROM catalog_candidates"
                ).fetchone()
                mappings = connection.execute(
                    "SELECT COUNT(*) FROM catalog_mappings WHERE status='approved'"
                ).fetchone()[0]

        self.assertEqual(
            observation,
            ("tesco", "water-5l", "1.45", None, None, "basketwatch", "EUR", "0.2900"),
        )
        self.assertEqual(result, ("observed", "basketwatch"))
        self.assertTrue(candidate[0].startswith("tesco:"))
        self.assertEqual(candidate[1], "pending_review")
        # Raw record is persisted scrubbed and inspectable.
        self.assertIn("Mystery Fizzy Drink", candidate[2])
        # No auto-approval happened.
        self.assertEqual(mappings, 1)

    def test_ingest_skips_invalid_prices_for_approved_mappings(self):
        bad_price = dict(MATCHING_RECORD, price="not-a-price")

        with tempfile.TemporaryDirectory() as directory:
            database = Path(directory) / "feed.sqlite"
            _seed_database(database)

            summary = ingest_basketwatch_snapshot(
                "tesco", database, "test-key",
                client_factory=lambda key: FakeClient([bad_price]),
            )

            self.assertEqual(summary["ingested"], 0)
            self.assertEqual(summary["skipped_invalid_price"], 1)
            with closing(sqlite3.connect(database)) as connection:
                observations = connection.execute(
                    "SELECT COUNT(*) FROM price_observations"
                ).fetchone()[0]
            self.assertEqual(observations, 0)

    def test_ingest_never_creates_observations_without_approved_mapping(self):
        with tempfile.TemporaryDirectory() as directory:
            database = Path(directory) / "feed.sqlite"
            _seed_database(database)
            with closing(sqlite3.connect(database)) as connection:
                connection.execute("DELETE FROM catalog_mappings")
                connection.commit()

            summary = ingest_basketwatch_snapshot(
                "tesco", database, "test-key",
                client_factory=lambda key: FakeClient([MATCHING_RECORD]),
            )

            self.assertEqual(summary["ingested"], 0)
            self.assertEqual(summary["queued_candidates"], 1)
            with closing(sqlite3.connect(database)) as connection:
                observations = connection.execute(
                    "SELECT COUNT(*) FROM price_observations"
                ).fetchone()[0]
                candidates = connection.execute(
                    "SELECT status FROM catalog_candidates"
                ).fetchall()
            self.assertEqual(observations, 0)
            self.assertEqual(candidates, [("pending_review",)])

    def test_ingest_on_missing_database_creates_schema(self):
        with tempfile.TemporaryDirectory() as directory:
            database = Path(directory) / "missing" / "feed.sqlite"
            summary = ingest_basketwatch_snapshot(
                "tesco", database, "test-key",
                client_factory=lambda key: FakeClient([]),
            )
            self.assertEqual(summary["fetched"], 0)
            self.assertTrue(database.exists())
            with closing(sqlite3.connect(database)) as connection:
                tables = {
                    row[0]
                    for row in connection.execute(
                        "SELECT name FROM sqlite_master WHERE type='table'"
                    )
                }
            self.assertIn("catalog_packs", tables)
            self.assertIn("collection_runs", tables)

    def test_ingest_failure_marks_run_failed_not_running(self):
        with tempfile.TemporaryDirectory() as directory:
            database = Path(directory) / "feed.sqlite"
            _seed_database(database)

            class BoomClient:
                def fetch_all(self, retailer_slug):
                    return [MATCHING_RECORD, UNMATCHED_RECORD]

            from beverage_feed import basketwatch as bw

            original = bw.normalize_listing
            calls = {"n": 0}

            def flaky(retailer, record):
                calls["n"] += 1
                if calls["n"] >= 2:
                    raise RuntimeError("boom mid-ingest")
                return original(retailer, record)

            with patch.object(bw, "normalize_listing", side_effect=flaky):
                with self.assertRaisesRegex(RuntimeError, "boom mid-ingest"):
                    ingest_basketwatch_snapshot(
                        "tesco", database, "test-key",
                        client_factory=lambda key: BoomClient(),
                    )

            with closing(sqlite3.connect(database)) as connection:
                run = connection.execute(
                    "SELECT status, failed_count FROM collection_runs"
                ).fetchone()
            self.assertEqual(run, ("failed", 1))


if __name__ == "__main__":
    unittest.main()
