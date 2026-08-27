import contextlib
import io
import json
import tempfile
import unittest
from pathlib import Path

from beverage_feed.discovery import DiscoveryStore, write_mappings, write_rejections
from beverage_feed.discovery_report import coverage_report, format_report, main as report_main


class CoverageReportTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.store = DiscoveryStore(Path(self.tmp.name) / "feed.sqlite")
        self.mapping_path = Path(self.tmp.name) / "mappings.json"
        self.rejection_path = Path(self.tmp.name) / "rejections.json"
        write_rejections(self.rejection_path, {"listings": [], "cells": []})

    def report(self, catalog_count=4, **kwargs):
        return coverage_report(
            self.store, catalog_count=catalog_count,
            mapping_path=self.mapping_path,
            retailers=("dunnes", "tesco"), now="2025-07-01T00:00:00Z",
            **kwargs,
        )

    def test_empty_matrix_reports_zero_coverage_with_full_active_denominator(self):
        report = self.report()
        dunnes = report["per_retailer"][0]
        self.assertEqual(dunnes["total_cells"], 4)
        self.assertEqual(dunnes["active"], 4)
        self.assertEqual(dunnes["coverage"], 0.0)
        self.assertEqual(report["overall"]["total_cells"], 8)

    def test_partial_matrix_counts_states_and_denominators(self):
        self.store.set_cell_state("dunnes", "p1", "approved", decided_by="discovery")
        self.store.set_cell_state("dunnes", "p2", "review", review_category="missing")
        self.store.set_cell_state("dunnes", "p3", "review", review_category="challenge")
        self.store.set_cell_state("dunnes", "p4", "inconclusive")
        report = self.report()
        dunnes = report["per_retailer"][0]
        self.assertEqual(dunnes["approved"], 1)
        self.assertEqual(dunnes["review"], 2)
        self.assertEqual(dunnes["review_missing"], 1)
        self.assertEqual(dunnes["review_challenge"], 1)
        self.assertEqual(dunnes["inconclusive"], 1)
        self.assertEqual(dunnes["active"], 4)
        self.assertEqual(dunnes["eligible"], 1)  # only approved is terminally decided
        self.assertEqual(dunnes["coverage"], 0.25)
        self.assertEqual(dunnes["inconclusive_rate"], 0.25)

    def test_explicit_exclusion_leaves_active_denominator(self):
        self.store.set_cell_state("dunnes", "p1", "approved", decided_by="discovery")
        self.store.set_cell_state("dunnes", "p2", "do_not_map")
        report = self.report()
        dunnes = report["per_retailer"][0]
        self.assertEqual(dunnes["do_not_map"], 1)
        self.assertEqual(dunnes["active"], 3)  # do-not-map excluded
        self.assertAlmostEqual(dunnes["coverage"], round(1 / 3, 4))
        # do_not_map is never counted as unmapped/availability evidence
        self.assertEqual(dunnes["unmapped"], 0)

    def test_resumed_pending_work_is_not_availability(self):
        self.store.set_cell_state("tesco", "p1", "pending", reason="source failure")
        self.store.set_cell_state("tesco", "p2", "identity_unstable")
        report = self.report()
        tesco = report["per_retailer"][1]
        self.assertEqual(tesco["pending"], 1)
        self.assertEqual(tesco["identity_unstable"], 1)
        self.assertEqual(tesco["eligible"], 0)  # neither is a terminal decision
        self.assertEqual(tesco["unmapped"], 0)
        self.assertNotIn("stock", report)
        self.assertNotIn("availability", report)

    def test_auto_approval_rate_uses_first_time_decisions_only(self):
        # First terminal decision per cell: auto-approved by discovery.
        self.store.set_cell_state("dunnes", "p1", "approved", decided_by="discovery")
        # Mature re-run: re-approval adds another transition, not a new first.
        self.store.set_cell_state("dunnes", "p1", "approved", decided_by="discovery")
        # Operator decision is not auto-approval.
        self.store.set_cell_state("dunnes", "p2", "approved", decided_by="alice")
        self.store.set_cell_state("dunnes", "p3", "unmapped", decided_by="discovery")
        report = self.report()
        dunnes = report["per_retailer"][0]
        self.assertEqual(dunnes["first_time_eligible_decisions"], 3)
        self.assertEqual(dunnes["first_time_auto_approved"], 1)
        self.assertAlmostEqual(dunnes["auto_approval_rate"], round(1 / 3, 4))

    def test_review_age_buckets_and_price_statuses_and_disagreement(self):
        self.store.set_cell_state(
            "dunnes", "p1", "review", review_category="conflicting-candidates",
            changed_at="2025-06-28T00:00:00Z",  # 3 days old -> 0-7d
        )
        self.store.set_cell_state(
            "dunnes", "p2", "review", review_category="missing",
            changed_at="2025-05-01T00:00:00Z",  # 61 days old -> >30d
        )
        self.store.upsert_candidate(
            "dunnes:a:a", retailer="dunnes", identity_key="a:a",
            identity_basis="product_reference:item_id", identity_tier="composite",
            source_product_reference="a", source_item_id="a", source_product_name="x",
        )
        self.store.record_evidence(
            "dunnes:a:a", "p1", retailer="dunnes",
            attribute_diffs={"unit_size_ml": {"structured": 330, "name": 500}},
            raw_price_value="oops", price_parse_status="malformed",
        )
        self.store.record_evidence(
            "dunnes:a:a", "p2", retailer="dunnes", attribute_diffs={},
            raw_price_value="1.20", price_parse_status="valid",
        )
        report = self.report()
        dunnes = report["per_retailer"][0]
        self.assertEqual(dunnes["review_age_buckets"], {"0-7d": 1, "7-30d": 0, ">30d": 1})
        self.assertEqual(dunnes["price_statuses"]["malformed"], 1)
        self.assertEqual(dunnes["price_statuses"]["valid"], 1)
        self.assertEqual(dunnes["disagreement_rate"], 0.5)
        # Overall aggregates dict-valued metrics too, never silent zeros.
        overall = report["overall"]
        self.assertEqual(overall["review_age_buckets"]["0-7d"], 1)
        self.assertEqual(overall["price_statuses"]["malformed"], 1)
        self.assertEqual(overall["price_statuses"]["valid"], 1)
        self.assertEqual(overall["disagreement_rate"], 0.5)

    def test_auto_approved_tiers_and_run_accounting(self):
        write_mappings(self.mapping_path, {"dunnes": [{
            "catalog_id": "p1", "expected_product_name": "x", "status": "approved",
            "auto_approved": True, "identity_tier": "composite",
            "source_product_reference": "a", "source_item_id": "a",
        }]})
        run_id = self.store.start_run("run-1")
        self.store.finish_run(
            run_id, "complete", request_counts={"search": 5, "hydration": 2},
            cells_advanced=3,
        )
        report = self.report()
        dunnes = report["per_retailer"][0]
        self.assertEqual(dunnes["auto_approved_tiers"], {"composite": 1})
        self.assertEqual(report["requests_consumed"], {"search": 5, "hydration": 2})
        self.assertEqual(report["cells_advanced"], 3)

    def test_mixed_retailer_overall_aggregates(self):
        self.store.set_cell_state("dunnes", "p1", "approved", decided_by="discovery")
        self.store.set_cell_state("tesco", "p1", "unmapped", decided_by="discovery")
        self.store.set_cell_state("tesco", "p2", "do_not_map")
        report = self.report()
        overall = report["overall"]
        self.assertEqual(overall["approved"], 1)
        self.assertEqual(overall["unmapped"], 1)
        self.assertEqual(overall["do_not_map"], 1)
        self.assertEqual(overall["total_cells"], 8)
        self.assertEqual(overall["active"], 7)
        self.assertEqual(overall["eligible"], 2)
        self.assertAlmostEqual(overall["coverage"], round(1 / 7, 4))


class ReportRenderingTests(unittest.TestCase):
    """Text and JSON rendering contract of the ``report`` CLI."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.store = DiscoveryStore(self.root / "feed.sqlite")
        self.mapping_path = self.root / "mappings.json"
        self.rejection_path = self.root / "rejections.json"
        write_rejections(self.rejection_path, {"listings": [], "cells": []})
        write_mappings(self.mapping_path, {"dunnes": []})

    def _report(self):
        return coverage_report(
            self.store, catalog_count=2, mapping_path=self.mapping_path,
            retailers=("dunnes",), now="2025-07-01T00:00:00Z",
        )

    def test_format_report_renders_header_rows_and_consumption_line(self):
        self.store.set_cell_state("dunnes", "p1", "approved", decided_by="discovery")
        report = self._report()
        report["requests_consumed"] = {"search": 5, "hydration": 2}
        text = format_report(report)
        lines = text.splitlines()
        self.assertTrue(lines[0].startswith("discovery coverage report generated="))
        self.assertIn("retailer total active approved coverage", lines[1])
        self.assertTrue(lines[2].startswith("dunnes 2 "))
        self.assertEqual(lines[2].split()[:4], ["dunnes", "2", "2", "1"])
        # Overall row is always the final metric row.
        self.assertTrue(lines[3].startswith("overall "))
        self.assertIn("requests_consumed=hydration=2,search=5", lines[4])
        self.assertIn("cells_advanced=0", lines[4])

    def test_format_report_omits_empty_consumption(self):
        lines = format_report(self._report()).splitlines()
        self.assertIn("requests_consumed=-", lines[-1])

    def test_main_json_output_is_parseable_and_exit_zero(self):
        catalog_path = self.root / "catalog.json"
        catalog_path.write_text(json.dumps([
            {
                "catalog_id": "pack-1", "name": "Cola 330ml Can",
                "brand": "Cola", "variant": "Original", "pack_count": 1,
                "unit_size_ml": 330, "package_type": "can",
                "search_term": "Cola 330ml",
            }
        ]) + "\n")
        stdout = io.StringIO()
        with contextlib.redirect_stdout(stdout):
            code = report_main([
                "--database", str(self.root / "feed.sqlite"),
                "--catalog", str(catalog_path),
                "--mapping", str(self.mapping_path),
                "--rejections", str(self.rejection_path),
                "--json",
            ])
        self.assertEqual(code, 0)
        report = json.loads(stdout.getvalue())
        self.assertIn("overall", report)
        # One catalog pack across the five default retailers.
        self.assertEqual(report["overall"]["total_cells"], 5)

    def test_main_table_output_contains_the_header(self):
        catalog_path = self.root / "catalog.json"
        catalog_path.write_text(json.dumps([]) + "\n")
        stdout = io.StringIO()
        with contextlib.redirect_stdout(stdout):
            code = report_main([
                "--database", str(self.root / "feed.sqlite"),
                "--catalog", str(catalog_path),
                "--mapping", str(self.mapping_path),
                "--rejections", str(self.rejection_path),
            ])
        self.assertEqual(code, 0)
        self.assertIn("retailer total active approved coverage", stdout.getvalue())


if __name__ == "__main__":
    unittest.main()
