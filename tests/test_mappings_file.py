"""Regression test: the committed real mapping file must stay schema-valid.

Found via full-feed-coverage ticket 17 — data/mappings.json drifted to a
pre-schema layout (supervalu rows with dunnes-style source keys, tesco rows
with an extra source field) and discovery validation failed silently until a
sprint refused to start. This test pins the real file to the validator.

R4 cutover: SQLite is the single writer and data/mappings.json is an export
artifact of ``export_mappings``. When the feed database is present, the
committed file must equal the export byte-for-byte — hand-editing either side
now fails here.
"""

import hashlib
import json
import sqlite3
import tempfile
import unittest
from contextlib import closing
from pathlib import Path

from beverage_feed.discovery import (
    _MAPPING_STATUSES,
    ensure_discovery_schema,
    export_mappings,
    load_mappings,
    write_mappings,
)

REPO_ROOT = Path(__file__).resolve().parent.parent
REPO_MAPPINGS = REPO_ROOT / "data" / "mappings.json"
REPO_DATABASE = REPO_ROOT / "data" / "feed.sqlite"
# Digest of the committed data/mappings.json. Bump only when a re-export is
# intentional (python -m beverage_feed export-mappings) and note it in the
# commit message. Keeps CI honest: the byte-for-byte SQLite pin below is
# skipped wherever the 36 MB feed database is absent.
MAPPINGS_SHA256 = "27c94bb586de0344f9276bbcc284719d251c1bea999e58fb1b4527a9d9505e54"


def _seed_database(database: str) -> None:
    ensure_discovery_schema(database)
    with closing(sqlite3.connect(database)) as connection:
        connection.executemany(
            "INSERT INTO catalog_packs VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            [
                ("pack-1", "Coke", "Coca-Cola", "original", 1, 500, "bottle", "x"),
                ("pack-2", "Zero", "Coca-Cola", "zero", 1, 330, "can", "y"),
            ],
        )
        connection.execute(
            "INSERT INTO catalog_mappings (catalog_id, retailer,"
            " expected_product_name, source_product_reference, source_item_id,"
            " status, decided_by, decided_at) VALUES"
            " ('pack-1', 'dunnes', 'Coke', '100', '100', 'approved',"
            "  'operator', '2026-09-03T20:00:00Z')"
        )
        connection.execute(
            "INSERT INTO catalog_mappings (catalog_id, retailer,"
            " expected_product_name, source_product_reference, status) VALUES"
            " ('pack-2', 'tesco', 'Zero', '92752847', 'approved')"
        )
        connection.commit()


class MappingsFileTests(unittest.TestCase):
    def test_committed_mappings_file_passes_schema_validation(self):
        mappings = load_mappings(REPO_MAPPINGS)
        self.assertTrue(mappings, "committed mappings file must not be empty")
        for retailer, rows in mappings.items():
            for row in rows:
                with self.subTest(retailer=retailer, catalog_id=row.get("catalog_id")):
                    self.assertIn(row["status"], _MAPPING_STATUSES)
                    self.assertTrue(row["expected_product_name"])

    def test_committed_mappings_file_digest_unchanged(self):
        self.assertEqual(
            hashlib.sha256(REPO_MAPPINGS.read_bytes()).hexdigest(),
            MAPPINGS_SHA256,
            "data/mappings.json changed without bumping MAPPINGS_SHA256 — "
            "intentional re-export? update the digest and say so in the commit",
        )

    @unittest.skipUnless(REPO_DATABASE.exists(), "feed database not present")
    def test_committed_mappings_file_matches_sqlite_export_byte_for_byte(self):
        with tempfile.TemporaryDirectory() as directory:
            exported = Path(directory) / "mappings.json"
            write_mappings(exported, export_mappings(REPO_DATABASE))
            self.assertEqual(
                exported.read_bytes(),
                REPO_MAPPINGS.read_bytes(),
                "data/mappings.json is stale — regenerate with: "
                "python -m beverage_feed export-mappings",
            )

    def test_export_is_deterministic_and_schema_valid(self):
        with tempfile.TemporaryDirectory() as directory:
            database = str(Path(directory) / "feed.sqlite")
            _seed_database(database)
            first = export_mappings(database)
            self.assertEqual(first, export_mappings(database))
            self.assertEqual(
                first,
                {
                    "dunnes": [
                        {
                            "catalog_id": "pack-1",
                            "expected_product_name": "Coke",
                            "source_product_reference": "100",
                            "source_item_id": "100",
                            "status": "approved",
                            "decided_by": "operator",
                            "decided_at": "2026-09-03T20:00:00Z",
                        }
                    ],
                    "tesco": [
                        {
                            "catalog_id": "pack-2",
                            "expected_product_name": "Zero",
                            "source_tpnb": "92752847",
                            "status": "approved",
                        }
                    ],
                },
            )
            # The export must round-trip through the validator unmodified.
            path = Path(directory) / "mappings.json"
            write_mappings(path, first)
            self.assertEqual(load_mappings(path), first)

    def test_write_mappings_uses_indent2_sorted_keys_plus_trailing_newline(self):
        with tempfile.TemporaryDirectory() as directory:
            database = str(Path(directory) / "feed.sqlite")
            _seed_database(database)
            mappings = export_mappings(database)
            path = Path(directory) / "mappings.json"
            write_mappings(path, mappings)
            self.assertEqual(
                path.read_text(),
                json.dumps(mappings, indent=2, sort_keys=True) + "\n",
            )

    def test_supervalu_rows_with_dunnes_source_keys_are_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "mappings.json"
            path.write_text(json.dumps({
                "supervalu": [{
                    "catalog_id": "pack-1",
                    "expected_product_name": "Coke",
                    "status": "approved",
                    "source_product_reference": "123:456",
                }],
            }))
            with self.assertRaises(ValueError):
                load_mappings(path)

    def test_tesco_rows_with_unknown_fields_are_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "mappings.json"
            path.write_text(json.dumps({
                "tesco": [{
                    "catalog_id": "pack-2",
                    "expected_product_name": "Zero",
                    "status": "approved",
                    "source_tpnb": "92752847",
                    "source_store_id": "2312",  # drifted field from ticket 17
                }],
            }))
            with self.assertRaises(ValueError):
                load_mappings(path)


if __name__ == "__main__":
    unittest.main()
