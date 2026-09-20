"""ff-15 R1b: mechanical retailer-block proposals (zero egress)."""

import json
import tempfile
import unittest
from pathlib import Path

from beverage_feed.discovery import DiscoveryStore, write_rejections
from beverage_feed.propose_blocks import (
    DECIDED_BY,
    apply_blocks,
    main,
    propose_blocks,
)


def _listing_rejection(
    retailer: str,
    candidate_id: str,
    cell: str,
    reason: str,
    *,
    state: str = "rejected",
) -> dict[str, object]:
    return {
        "canonical_key": candidate_id,
        "retailer": retailer,
        "cell": cell,
        "rejected_at": "2026-09-20T10:00:00Z",
        "decided_by": "agent-sprint",
        "reason": reason,
        "state": state,
    }


class ProposeBlocksTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name)
        self.rejections_path = self.root / "rejections.json"
        self.database = self.root / "feed.sqlite"

    def _write(self, listings: list[dict[str, object]]) -> None:
        write_rejections(self.rejections_path, {"listings": listings, "cells": []})

    def test_multi_cell_junk_candidate_is_proposed_with_evidence(self) -> None:
        self._write([
            _listing_rejection("dunnes", "dunnes:1:2", "cell-a",
                               "agent-sprint: cola-flavoured sweet/lolly, wrong brand"),
            _listing_rejection("dunnes", "dunnes:1:2", "cell-b",
                               "agent-sprint: cola-flavoured sweet/lolly, wrong brand"),
        ])
        report = propose_blocks(self.rejections_path)
        self.assertEqual(report["counts"]["junk_proposals"], 1)
        proposal = report["junk_proposals"][0]
        self.assertEqual(proposal["retailer"], "dunnes")
        self.assertEqual(proposal["candidate_id"], "dunnes:1:2")
        self.assertEqual(proposal["rejected_cells"], 2)
        self.assertIn("sweet", proposal["reasons"][0])

    def test_variant_dispute_is_never_proposed_for_auto_apply(self) -> None:
        """Variant-of-same-brand stays per-cell: Feilim batch, never a block."""
        self._write([
            _listing_rejection("dunnes", "dunnes:9:8", "cell-a",
                               "agent-sprint: wrong variant (Zero)"),
            _listing_rejection("dunnes", "dunnes:9:8", "cell-b",
                               "agent-sprint: wrong size 500ml"),
            _listing_rejection("dunnes", "dunnes:9:8", "cell-c",
                               "agent-sprint: wrong flavour (Lemon)"),
        ])
        report = propose_blocks(self.rejections_path)
        self.assertEqual(report["counts"]["junk_proposals"], 0)
        self.assertEqual(report["counts"]["variant_ambiguous"], 1)
        self.assertEqual(len(report["variant_ambiguous"][0]["reasons"]), 3)

    def test_single_cell_rejection_is_never_proposed(self) -> None:
        self._write([
            _listing_rejection("dunnes", "dunnes:1:2", "cell-a",
                               "agent-sprint: keyword noise match"),
        ])
        report = propose_blocks(self.rejections_path)
        self.assertEqual(report["counts"]["junk_proposals"], 0)

    def test_group_with_one_variant_reason_is_not_unambiguous(self) -> None:
        """A junk group where any row disputes the variant stays in the batch."""
        self._write([
            _listing_rejection("dunnes", "dunnes:5:5", "cell-a",
                               "agent-sprint: keyword noise match"),
            _listing_rejection("dunnes", "dunnes:5:5", "cell-b",
                               "agent-sprint: wrong size 2L"),
        ])
        report = propose_blocks(self.rejections_path)
        self.assertEqual(report["counts"]["junk_proposals"], 0)
        self.assertEqual(report["counts"]["variant_ambiguous"], 1)

    def test_apply_writes_retailer_block_json_through_the_real_seam(self) -> None:
        self.store = DiscoveryStore(self.database)
        self.store.upsert_candidate(
            "dunnes:1:2",
            retailer="dunnes",
            identity_key="1:2",
            identity_basis="composite",
            identity_tier="product",
            source_product_name="Cola Sweets 100g Bag",
        )
        for cell in ("cell-a", "cell-b"):
            self.store.associate_candidate("dunnes:1:2", cell, "Diet Coke", retailer="dunnes")
        self._write([
            _listing_rejection("dunnes", "dunnes:1:2", "cell-a",
                               "agent-sprint: cola-flavoured sweet/lolly, wrong brand"),
            _listing_rejection("dunnes", "dunnes:1:2", "cell-b",
                               "agent-sprint: cola-flavoured sweet/lolly, wrong brand"),
        ])
        report = propose_blocks(self.rejections_path)
        outcome = apply_blocks(
            report, rejection_path=self.rejections_path, database=self.database,
        )
        self.assertEqual(outcome["applied_count"], 1)
        self.assertEqual(outcome["cells_cleared_total"], 2)
        rejections = json.loads(self.rejections_path.read_text())
        record = rejections["retailer_blocks"][0]
        self.assertEqual(record["canonical_key"], "dunnes:1:2")
        self.assertEqual(record["state"], "blocked")
        self.assertEqual(record["decided_by"], DECIDED_BY)
        status = self.store.connection().execute(
            "SELECT status FROM catalog_candidates WHERE candidate_id='dunnes:1:2'"
        ).fetchone()
        self.assertEqual(status, ("rejected",))

    def test_main_reports_counts_and_writes_the_markdown_batch(self) -> None:
        self._write([
            _listing_rejection("dunnes", "dunnes:1:2", "cell-a",
                               "agent-sprint: keyword noise match"),
            _listing_rejection("dunnes", "dunnes:1:2", "cell-b",
                               "agent-sprint: keyword noise match"),
        ])
        report_path = self.root / "report.md"
        code = main([
            "--rejections", str(self.rejections_path),
            "--database", str(self.database),
            "--report", str(report_path),
        ])
        self.assertEqual(code, 0)
        text = report_path.read_text()
        self.assertIn("# Retailer block proposals —", text)
        self.assertIn("unambiguous junk proposals: **1**", text)
        self.assertIn("`dunnes:1:2`", text)


if __name__ == "__main__":
    unittest.main()
