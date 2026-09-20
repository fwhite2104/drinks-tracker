"""Tests for beverage_feed.catalog_ruling (R2 decision pack, read-only)."""

from __future__ import annotations

import json
import sqlite3
import tempfile
import unittest
from contextlib import closing
from pathlib import Path
from types import SimpleNamespace
from typing import Any

from beverage_feed.catalog_ruling import (
    RETAILERS,
    _collect_variant_evidence,
    build_ruling_pack,
    render_markdown,
)

SCHEMA = """
CREATE TABLE catalog_candidates (
    candidate_id INTEGER PRIMARY KEY,
    retailer TEXT NOT NULL,
    source_product_name TEXT,
    raw_record TEXT,
    status TEXT NOT NULL DEFAULT 'pending_review'
);
CREATE TABLE catalog_mappings (
    catalog_id TEXT NOT NULL,
    retailer TEXT NOT NULL,
    status TEXT NOT NULL
);
CREATE TABLE discovery_rejections (
    section TEXT NOT NULL,
    retailer TEXT NOT NULL,
    catalog_id TEXT,
    state TEXT NOT NULL
);
CREATE TABLE discovery_candidate_cells (
    candidate_id INTEGER,
    retailer TEXT NOT NULL,
    catalog_id TEXT,
    first_seen_at TEXT,
    last_seen_at TEXT,
    search_terms TEXT
);
"""

CATALOG = [
    {
        "catalog_id": "coca-zero-330",
        "name": "Coca-Cola Zero 330ml Can",
        "brand": "Coca-Cola",
        "variant": "Zero",
        "pack_count": 1,
        "unit_size_ml": 330,
        "package_type": "can",
        "search_term": "Coca-Cola Zero 330ml Can",
        "aliases": ["Coke Zero"],
    },
    {
        "catalog_id": "fanta-orange-330",
        "name": "Fanta Orange 330ml Can",
        "brand": "Fanta",
        "variant": "Orange",
        "pack_count": 1,
        "unit_size_ml": 330,
        "package_type": "can",
        "search_term": "Fanta Orange 330ml Can",
        "aliases": [],
    },
]


class RulingPackTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.database = Path(self._tmp.name) / "feed.sqlite"
        self.catalog = Path(self._tmp.name) / "catalog.json"
        self.catalog.write_text(json.dumps(CATALOG))
        with closing(sqlite3.connect(self.database)) as connection:
            connection.executescript(SCHEMA)
            connection.executemany(
                "INSERT INTO catalog_candidates (retailer, source_product_name, status) "
                "VALUES (?, ?, ?)",
                [
                    # Variant-named listing -> becomes a family.
                    ("dunnes", "Fanta Zero Sugar 500ml", "pending_review"),
                    ("dunnes", "Fanta Zero Sugar 2 Litres", "pending_review"),
                    # Names an existing catalog variant phrasing -> no family.
                    ("supervalu", "Fanta Orange 330ml Can", "pending_review"),
                    # Alias phrasing of an existing catalog pack -> no family.
                    ("dunnes", "Coke Zero 330ml", "pending_review"),
                    # Rejected listing -> excluded even when variant-named.
                    ("tesco", "Fanta Zero Sugar 250ml", "rejected"),
                ],
            )
            connection.executemany(
                "INSERT INTO catalog_mappings (catalog_id, retailer, status) VALUES (?, ?, ?)",
                [
                    ("coca-zero-330", "dunnes", "approved"),
                    ("coca-zero-330", "supervalu", "approved"),
                    ("fanta-orange-330", "dunnes", "approved"),
                ],
            )
            connection.executemany(
                "INSERT INTO discovery_rejections (section, retailer, catalog_id, state) "
                "VALUES (?, ?, ?, ?)",
                [
                    ("listings", "tesco", "coca-zero-330", "rejected"),
                    ("listings", "tesco", "fanta-orange-330", "rejected"),
                    ("cells", "tesco", "fanta-orange-330", "do_not_map"),
                ],
            )
            connection.executemany(
                "INSERT INTO discovery_candidate_cells (retailer, catalog_id) VALUES (?, ?)",
                [("lidl", "fanta-orange-330"), ("lidl", "coca-zero-330")],
            )
            connection.commit()
        self.before = self.database.read_bytes()

    def _build(self) -> dict[str, Any]:
        return build_ruling_pack(self.catalog, self.database)

    def test_variant_named_listing_becomes_family(self) -> None:
        variants = {entry["family"]: entry for entry in self._build()["variant_evidence"]}
        self.assertIn("fanta: zero", variants)
        self.assertEqual(variants["fanta: zero"]["listings"], 2)
        self.assertEqual(variants["fanta: zero"]["retailers"], ["dunnes"])

    def test_listing_matching_existing_catalog_variant_is_not_a_family(self) -> None:
        families = [entry["family"] for entry in self._build()["variant_evidence"]]
        self.assertNotIn("fanta: orange", families)
        # Coke Zero states the catalog alias phrasing: no coca family either.
        self.assertFalse(
            [family for family in families if family.startswith("coca")]
        )

    def test_rejected_listings_are_excluded_from_variant_evidence(self) -> None:
        variants = {entry["family"]: entry for entry in self._build()["variant_evidence"]}
        # The rejected Tesco listing never contributes: no Tesco retailer on
        # any family row.
        for entry in variants.values():
            self.assertNotIn("tesco", entry["retailers"])

    def test_cell_cost_arithmetic_excludes_approved_and_do_not_map(self) -> None:
        pack = self._build()
        drafts = pack["drafts"]
        # fanta-orange-330: dunnes approved, tesco do_not_map
        # -> open cells are the other three retailers.
        rows = pack["rows"]
        by_id = {row["catalog_id"]: row for row in rows}
        self.assertEqual(
            set(by_id["fanta-orange-330"]["open_cells"]),
            {"supervalu", "lidl", "aldi"},
        )
        # coca-zero-330 has 2 approved mappings -> proven -> in the core draft.
        self.assertIn("coca-zero-330", drafts["a_comparable_core"]["catalog_ids"])
        self.assertEqual(
            drafts["a_comparable_core"]["cells_left"],
            sum(len(row["open_cells"]) for row in rows
                if row["catalog_id"] in set(drafts["a_comparable_core"]["catalog_ids"])),
        )
        self.assertEqual(
            drafts["b_stay_at_100"]["cells_left"],
            sum(len(row["open_cells"]) for row in rows),
        )

    def test_core_draft_picks_proven_packs_first(self) -> None:
        pack = self._build()
        core_ids = pack["drafts"]["a_comparable_core"]["catalog_ids"]
        rows = {row["catalog_id"]: row for row in pack["rows"]}
        proven = [
            cid for cid, row in rows.items() if len(row["approved_retailers"]) >= 2
        ]
        for catalog_id in proven:
            self.assertIn(catalog_id, core_ids)
        # Only proven/comparable packs make the core.
        self.assertTrue(
            all(rows[cid]["evidence_retailers"] >= 2 for cid in core_ids)
        )

    def test_variant_expanded_draft_extends_core_with_new_packs(self) -> None:
        drafts = self._build()["drafts"]
        draft = drafts["c_variant_expanded"]
        self.assertEqual(len(draft["new_packs"]), 10)
        self.assertEqual(
            draft["cells_left"],
            drafts["a_comparable_core"]["cells_left"]
            + len(draft["new_packs"]) * len(RETAILERS),
        )

    def test_build_is_read_only(self) -> None:
        self._build()
        self.assertEqual(self.database.read_bytes(), self.before)

    def test_markdown_renders_decision_pack(self) -> None:
        text = render_markdown(self._build())
        self.assertIn("# Catalog ruling pack", text)
        self.assertIn("Feilim rules", text)
        self.assertIn("no `data/catalog.json` edit happened", text)
        self.assertIn("| dunnes |", text)
        self.assertIn("fanta: zero", text)


class VariantEvidenceUnitTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.database = Path(self._tmp.name) / "feed.sqlite"
        with closing(sqlite3.connect(self.database)) as connection:
            connection.executescript(SCHEMA)
            connection.executemany(
                "INSERT INTO catalog_candidates (retailer, source_product_name, status) "
                "VALUES (?, ?, ?)",
                [("dunnes", "Fanta Zero Sugar 330ml", "pending_review")],
            )
            connection.commit()

    def test_variant_evidence_reads_distinct_listing_names(self) -> None:
        fake_pack = SimpleNamespace(brand="Fanta", variant="Orange", aliases=())
        with closing(sqlite3.connect(self.database)) as connection:
            rows = _collect_variant_evidence(connection, [fake_pack])
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["listings"], 1)
        self.assertEqual(rows[0]["example_names"], ["Fanta Zero Sugar 330ml"])


if __name__ == "__main__":
    unittest.main()
