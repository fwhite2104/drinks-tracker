"""Regression test: the committed real mapping file must stay schema-valid.

Found via full-feed-coverage ticket 17 — data/mappings.json drifted to a
pre-schema layout (supervalu rows with dunnes-style source keys, tesco rows
with an extra source field) and discovery validation failed silently until a
sprint refused to start. This test pins the real file to the validator.
"""

import unittest
from pathlib import Path

from beverage_feed.discovery import _MAPPING_STATUSES, load_mappings

REPO_MAPPINGS = Path(__file__).resolve().parent.parent / "data" / "mappings.json"


class MappingsFileTests(unittest.TestCase):
    def test_committed_mappings_file_passes_schema_validation(self):
        mappings = load_mappings(REPO_MAPPINGS)
        self.assertTrue(mappings, "committed mappings file must not be empty")
        for retailer, rows in mappings.items():
            for row in rows:
                with self.subTest(retailer=retailer, catalog_id=row.get("catalog_id")):
                    self.assertIn(row["status"], _MAPPING_STATUSES)
                    self.assertTrue(row["expected_product_name"])


if __name__ == "__main__":
    unittest.main()
