"""Tests for the evidence classification pass over discovery evidence (ticket 13).

Every candidate-cell's latest evidence is classified into the trial classes
A-D from ticket 04, with the universal junk gate applied so junk rows are set
aside rather than classified.  All tests are hermetic: a temp DiscoveryStore,
a fixture catalog, no live retailer calls.
"""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from beverage_feed.collector import BenchmarkPack
from beverage_feed.discovery import DiscoveryStore
from beverage_feed.discovery_classify import (
    classify_candidate_cell,
    classify_evidence,
    format_classification,
)


def _pack(
    catalog_id: str,
    name: str,
    brand: str,
    variant: str,
    *,
    aliases: tuple[str, ...] = (),
    size: int = 330,
    count: int = 1,
    package: str = "can",
) -> BenchmarkPack:
    return BenchmarkPack(
        catalog_id=catalog_id,
        name=name,
        brand=brand,
        variant=variant,
        pack_count=count,
        unit_size_ml=size,
        package_type=package,
        search_term=name,
        aliases=aliases,
    )


CATALOG = [
    _pack(
        "coca-original-330", "Coca-Cola Original Taste 330ml Can", "Coca-Cola",
        "Original Taste", aliases=("Coke Original",),
    ),
    _pack(
        "coca-diet-330", "Diet Coke 330ml Can", "Coca-Cola", "Diet",
        aliases=("Diet Coke",),
    ),
    _pack("fanta-orange-330", "Fanta Orange 330ml Can", "Fanta", "Orange"),
    _pack(
        "coca-diet-2000", "Coca-Cola Diet 2L Bottle", "Coca-Cola", "Diet",
        aliases=("Diet Coke",), size=2000, package="bottle",
    ),
]


def _dunnes_record(name: str, **extra: object) -> dict[str, object]:
    record: dict[str, object] = {
        "productName": name, "productReference": "111", "itemId": "222",
        "price": 2.5,
    }
    record.update(extra)
    return record


class ClassificationPassTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.database = Path(self._tmp.name) / "discovery.sqlite"
        self.store = DiscoveryStore(self.database)

    def _seed(
        self,
        *,
        candidate_id: str,
        name: str,
        catalog_id: str,
        retailer: str = "dunnes",
        raw_record: object | None = None,
        normalized: dict[str, object] | None = None,
        basis: dict[str, str] | None = None,
        diffs: dict[str, object] | None = None,
        price_status: str = "valid",
        price_value: str | None = "2.50",
        cell_state: str | None = None,
    ) -> None:
        self.store.upsert_candidate(
            candidate_id,
            retailer=retailer,
            identity_key=candidate_id,
            identity_basis="test",
            identity_tier="product",
            source_product_name=name,
            raw_record=raw_record,
        )
        if cell_state is not None:
            self.store.set_cell_state(
                retailer, catalog_id, cell_state, decided_by="test",
            )
        self.store.associate_candidate(candidate_id, catalog_id, "test term", retailer=retailer)
        self.store.record_evidence(
            candidate_id, catalog_id,
            retailer=retailer,
            raw_attributes={"name": name},
            normalized_attributes=normalized or {},
            inference_basis=basis or {},
            attribute_diffs=diffs or {},
            raw_price_value=price_value,
            price_parse_status=price_status,
        )

    def _classify(self, **kwargs: object) -> dict[str, object]:
        return classify_evidence(list(CATALOG), self.store, **kwargs)  # type: ignore[arg-type]

    # -- class A ------------------------------------------------------------

    def test_clean_alias_candidate_classifies_a_into_the_sprint_batch(self) -> None:
        # The Brand Alias layer translates "Diet Coke" at extraction time, so
        # the re-run matcher sees brand Coca-Cola + variant Diet, all
        # name-derived, unique attribute candidate, price valid.
        self._seed(
            candidate_id="dunnes:111:222",
            name="Diet Coke 330ml Can",
            catalog_id="coca-diet-330",
            raw_record=_dunnes_record("Diet Coke 330ml Can"),
            cell_state="pending",
        )
        report = self._classify()
        counts = report["counts"]["candidate_cells"]  # type: ignore[index]
        self.assertEqual(counts["A"], 1)
        self.assertEqual(counts["total"], 1)
        batch = report["batches"]["A"]  # type: ignore[index]
        self.assertEqual(len(batch), 1)
        entry = batch[0]
        self.assertEqual(entry["candidate_id"], "dunnes:111:222")
        self.assertEqual(entry["class"], "A")
        self.assertEqual(entry["reasons"], [])
        self.assertEqual(entry["cell_class"], "A")
        self.assertIsNotNone(entry["price"])
        self.assertEqual(report["spot_check"], batch)  # type: ignore[index]
        self.assertEqual(report["counts"]["cells"]["A"], 1)  # type: ignore[index]

    def test_class_a_batch_spot_check_samples_about_ten_percent(self) -> None:
        for index in range(25):
            self._seed(
                candidate_id=f"dunnes:{index}:222",
                name="Diet Coke 330ml Can",
                catalog_id="coca-diet-330",
                raw_record=_dunnes_record("Diet Coke 330ml Can", productReference=str(index)),
            )
        report = self._classify()
        batch = report["batches"]["A"]  # type: ignore[index]
        self.assertEqual(len(batch), 25)
        sample = report["spot_check"]  # type: ignore[index]
        self.assertEqual(len(sample), 2)  # max(1, 25 // 10), evenly spaced
        self.assertEqual(
            [entry["candidate_id"] for entry in sample],
            [batch[index * 25 // 2]["candidate_id"] for index in range(2)],
        )

    # -- excluded (junk gate) -----------------------------------------------

    def test_junk_evidence_is_set_aside_rather_than_classified(self) -> None:
        self._seed(
            candidate_id="dunnes:333:444",
            name="Kenwood Ripple Pure White 1.5L Kettle",
            catalog_id="coca-original-330",
            raw_record=_dunnes_record("Kenwood Ripple Pure White 1.5L Kettle", price=34.99),
            cell_state="pending",
        )
        report = self._classify()
        counts = report["counts"]["candidate_cells"]  # type: ignore[index]
        self.assertEqual(counts["excluded"], 1)
        self.assertEqual(counts["total"], 1)
        for classification in ("A", "B", "C", "D"):
            self.assertEqual(report["batches"][classification], [])  # type: ignore[index]
        self.assertEqual(len(report["excluded"]), 1)  # type: ignore[index]
        # No classifiable evidence on a non-terminal cell: it is thin.
        self.assertEqual(report["counts"]["cells"]["unclassified"], 1)  # type: ignore[index]
        self.assertEqual(
            [cell["catalog_id"] for cell in report["thin_cells"]],  # type: ignore[index]
            ["coca-original-330"],
        )
        self.assertEqual(
            [cell["catalog_id"] for cell in report["rerun_targets"]],  # type: ignore[index]
            ["coca-original-330"],
        )

    # -- class C ------------------------------------------------------------

    def test_ambiguous_attributes_matching_multiple_packs_classify_c(self) -> None:
        # No brand/variant stated: size/count/package match both Coke packs.
        self._seed(
            candidate_id="dunnes:555:666",
            name="Cola 330ml Can",
            catalog_id="coca-original-330",
            raw_record=_dunnes_record("Cola 330ml Can"),
        )
        report = self._classify()
        self.assertEqual(report["counts"]["candidate_cells"]["C"], 1)  # type: ignore[index]
        entry = report["batches"]["C"][0]  # type: ignore[index]
        self.assertIn("multiple catalog packs", entry["reasons"][0])  # type: ignore[index]

    def test_structured_vs_name_conflicts_classify_c(self) -> None:
        # Structured total volume (990ml) conflicts with the name's 330ml on
        # a uniquely-attribute-matched (Fanta) candidate.
        self._seed(
            candidate_id="dunnes:777:888",
            name="Fanta Orange 330ml Can",
            catalog_id="fanta-orange-330",
            raw_record=_dunnes_record(
                "Fanta Orange 330ml Can", totalVolumeMl=990,
            ),
        )
        report = self._classify()
        self.assertEqual(report["counts"]["candidate_cells"]["C"], 1)  # type: ignore[index]
        entry = report["batches"]["C"][0]  # type: ignore[index]
        self.assertIn("conflicts", entry["reasons"][0])  # type: ignore[index]

    def test_clean_candidates_keep_the_cell_batch_ready(self) -> None:
        for reference in ("111", "999"):
            self._seed(
                candidate_id=f"dunnes:{reference}:222",
                name="Diet Coke 330ml Can",
                catalog_id="coca-diet-330",
                raw_record=_dunnes_record("Diet Coke 330ml Can", productReference=reference),
            )
        report = self._classify()
        self.assertEqual(report["counts"]["candidate_cells"]["A"], 2)  # type: ignore[index]
        self.assertEqual(report["counts"]["cells"]["A"], 1)  # type: ignore[index]
        for entry in report["batches"]["A"]:  # type: ignore[index]
            self.assertEqual(entry["cell_class"], "A")

    def test_clean_cell_stays_batch_ready_with_priceless_siblings(self) -> None:
        # A same-pack listing without a price does not block the clean one.
        self._seed(
            candidate_id="dunnes:111:222",
            name="Diet Coke 330ml Can",
            catalog_id="coca-diet-330",
            raw_record=_dunnes_record("Diet Coke 330ml Can"),
        )
        self._seed(
            candidate_id="dunnes:999:222",
            name="Diet Coke 330ml Can",
            catalog_id="coca-diet-330",
            # No price in the raw record: re-extraction yields price missing.
            raw_record=_dunnes_record("Diet Coke 330ml Can", productReference="999", price=None),
        )
        report = self._classify()
        self.assertEqual(report["counts"]["cells"]["A"], 1)  # type: ignore[index]
        self.assertEqual(report["counts"]["cells"]["D"], 0)  # type: ignore[index]

    def test_distinct_clean_candidates_conflict_the_cell_into_c(self) -> None:
        # Two clean candidates naming different products: which one is the
        # mapping?  Conflicting-candidates → per-item.
        for name in ("Diet Coke 330ml Can", "Diet Coke 330ml"):
            self._seed(
                candidate_id=f"dunnes:{abs(hash(name)) % 1000000}:222",
                name=name,
                catalog_id="coca-diet-330",
                raw_record=_dunnes_record(name, productReference=str(abs(hash(name)) % 1000000)),
            )
        report = self._classify()
        self.assertEqual(report["counts"]["candidate_cells"]["A"], 2)  # type: ignore[index]
        self.assertEqual(report["counts"]["cells"]["C"], 1)  # type: ignore[index]

    def test_ambiguous_candidate_demotes_a_clean_cell_to_c(self) -> None:
        self._seed(
            candidate_id="dunnes:111:222",
            name="Diet Coke 330ml Can",
            catalog_id="coca-diet-330",
            raw_record=_dunnes_record("Diet Coke 330ml Can"),
        )
        self._seed(
            candidate_id="dunnes:999:222",
            name="Cola 330ml Can",
            catalog_id="coca-diet-330",
            raw_record=_dunnes_record("Cola 330ml Can", productReference="999"),
        )
        report = self._classify()
        self.assertEqual(report["counts"]["cells"]["C"], 1)  # type: ignore[index]
        self.assertEqual(report["counts"]["cells"]["A"], 0)  # type: ignore[index]

    # -- class B ------------------------------------------------------------

    def test_attributes_matching_no_catalog_pack_classify_b(self) -> None:
        # No pack in the catalog is a 500ml can, so the listing's attributes
        # (brand via alias, 500ml, single can) match nothing.
        self._seed(
            candidate_id="dunnes:AAA:222",
            name="Coca-Cola Cherry 500ml Can",
            catalog_id="coca-original-330",
            raw_record=_dunnes_record("Coca-Cola Cherry 500ml Can"),
        )
        report = self._classify()
        self.assertEqual(report["counts"]["candidate_cells"]["B"], 1)  # type: ignore[index]
        entry = report["batches"]["B"][0]  # type: ignore[index]
        self.assertIn("match no catalog pack", entry["reasons"][0])  # type: ignore[index]

    def test_name_disagreement_on_unique_attributes_classifies_b(self) -> None:
        # Structured brand+variant agree with the Diet pack uniquely, but the
        # listing name never says "Diet Coke": per-item eyeball.
        self._seed(
            candidate_id="dunnes:BBB:222",
            name="Coca-Cola Diet 330ml Can",
            catalog_id="coca-diet-330",
            raw_record=_dunnes_record(
                "Coca-Cola Diet 330ml Can", brand="Coca-Cola", variant="Diet",
            ),
        )
        report = self._classify()
        self.assertEqual(report["counts"]["candidate_cells"]["B"], 1)  # type: ignore[index]
        entry = report["batches"]["B"][0]  # type: ignore[index]
        self.assertIn("does not contain the pack name", entry["reasons"][0])  # type: ignore[index]

    def test_unique_match_to_another_pack_classifies_b_on_this_cell(self) -> None:
        # "Diet Coke 2L" uniquely matches the 2L Diet pack; on the 330ml
        # single-can cell (where a loose search attached it) it is per-item.
        self._seed(
            candidate_id="dunnes:NNN:222",
            name="Diet Coke 2L Bottle",
            catalog_id="coca-diet-330",
            raw_record=_dunnes_record("Diet Coke 2L Bottle", price=3.4),
        )
        report = self._classify()
        entry = report["batches"]["B"][0]  # type: ignore[index]
        self.assertEqual(entry["candidate_id"], "dunnes:NNN:222")  # type: ignore[index]
        self.assertIn("another catalog pack: coca-diet-2000", entry["reasons"][0])  # type: ignore[index]

    def test_structured_basis_blocks_class_a(self) -> None:
        # The brand is stated structurally, not inferred from the listing
        # name, so the trial's clean bar demotes it to per-item review.
        self._seed(
            candidate_id="dunnes:CCC:222",
            name="Diet Coke 330ml Can",
            catalog_id="coca-diet-330",
            raw_record=_dunnes_record("Diet Coke 330ml Can", brand="Coca-Cola"),
        )
        report = self._classify()
        self.assertEqual(report["counts"]["candidate_cells"]["B"], 1)  # type: ignore[index]
        entry = report["batches"]["B"][0]  # type: ignore[index]
        self.assertIn("not inferred from the listing name", entry["reasons"][0])  # type: ignore[index]

    # -- class D ------------------------------------------------------------

    def test_missing_price_classifies_d_and_targets_the_rerun(self) -> None:
        self._seed(
            candidate_id="dunnes:DDD:222",
            name="Diet Coke 330ml Can",
            catalog_id="coca-diet-330",
            raw_record=_dunnes_record("Diet Coke 330ml Can", price=None),
            price_status="missing",
            price_value=None,
        )
        report = self._classify()
        self.assertEqual(report["counts"]["candidate_cells"]["D"], 1)  # type: ignore[index]
        entry = report["batches"]["D"][0]  # type: ignore[index]
        self.assertIn("price", entry["reasons"][0])  # type: ignore[index]
        target = report["rerun_targets"][0]  # type: ignore[index]
        self.assertEqual(target["catalog_id"], "coca-diet-330")
        self.assertIn("class D", target["reason"])

    def test_class_d_cells_with_terminal_states_are_not_rerun_targets(self) -> None:
        self._seed(
            candidate_id="dunnes:EEE:222",
            name="Diet Coke 330ml Can",
            catalog_id="coca-diet-330",
            raw_record=_dunnes_record("Diet Coke 330ml Can", price=None),
            price_status="missing",
            price_value=None,
            cell_state="approved",
        )
        report = self._classify()
        self.assertEqual(report["counts"]["candidate_cells"]["D"], 1)  # type: ignore[index]
        self.assertEqual(report["rerun_targets"], [])  # type: ignore[index]

    # -- thin cells ---------------------------------------------------------

    def test_inconclusive_cell_without_evidence_is_thin(self) -> None:
        self.store.set_cell_state(
            "dunnes", "fanta-orange-330", "inconclusive", decided_by="test",
        )
        report = self._classify()
        self.assertEqual(len(report["thin_cells"]), 1)  # type: ignore[index]
        cell = report["thin_cells"][0]  # type: ignore[index]
        self.assertEqual(cell["state"], "inconclusive")
        self.assertEqual(
            [cell["catalog_id"] for cell in report["rerun_targets"]],  # type: ignore[index]
            ["fanta-orange-330"],
        )

    def test_terminal_cells_are_never_thin(self) -> None:
        self.store.set_cell_state(
            "dunnes", "fanta-orange-330", "approved", decided_by="test",
        )
        report = self._classify()
        self.assertEqual(report["thin_cells"], [])  # type: ignore[index]
        self.assertEqual(report["rerun_targets"], [])  # type: ignore[index]

    # -- evidence selection -------------------------------------------------

    def test_latest_evidence_row_wins_for_a_candidate_cell(self) -> None:
        self._seed(
            candidate_id="dunnes:FFF:222",
            name="Diet Coke 330ml Can",
            catalog_id="coca-diet-330",
            normalized={"pack_count": 5, "unit_size_ml": 330},
            basis={"pack_count": "name", "unit_size_ml": "name"},
        )
        self.store.record_evidence(
            "dunnes:FFF:222", "coca-diet-330",
            retailer="dunnes",
            raw_attributes={"name": "Diet Coke 330ml Can"},
            normalized_attributes={
                "brand": "Coca-Cola", "variant": "Diet",
                "pack_count": 1, "unit_size_ml": 330, "package_type": "can",
            },
            inference_basis={
                "brand": "name", "variant": "name", "pack_count": "name",
                "unit_size_ml": "name", "package_type": "name",
            },
            attribute_diffs={},
            raw_price_value="2.50",
            price_parse_status="valid",
        )
        report = self._classify()
        self.assertEqual(report["counts"]["candidate_cells"]["A"], 1)  # type: ignore[index]

    def test_fallback_uses_stored_evidence_when_raw_record_is_missing(self) -> None:
        self._seed(
            candidate_id="dunnes:GGG:222",
            name="Diet Coke 330ml Can",
            catalog_id="coca-diet-330",
            raw_record=None,
            normalized={
                "brand": "Coca-Cola", "variant": "Diet",
                "pack_count": 1, "unit_size_ml": 330, "package_type": "can",
            },
            basis={
                "brand": "name", "variant": "name", "pack_count": "name",
                "unit_size_ml": "name", "package_type": "name",
            },
        )
        report = self._classify()
        self.assertEqual(report["counts"]["candidate_cells"]["A"], 1)  # type: ignore[index]

    def test_retailer_filter_restricts_the_classification(self) -> None:
        self._seed(
            candidate_id="dunnes:HHH:222",
            name="Diet Coke 330ml Can",
            catalog_id="coca-diet-330",
            raw_record=_dunnes_record("Diet Coke 330ml Can"),
        )
        self._seed(
            candidate_id="supervalu:JJJ",
            name="Diet Coke 330ml Can",
            catalog_id="coca-diet-330",
            retailer="supervalu",
            raw_record={"productName": "Diet Coke 330ml Can", "productId": "JJJ", "price": 2.6},
        )
        report = self._classify(retailer="dunnes")
        self.assertEqual(report["counts"]["candidate_cells"]["total"], 1)  # type: ignore[index]
        self.assertEqual(report["counts"]["candidate_cells"]["A"], 1)  # type: ignore[index]

    def test_evidence_for_unknown_catalog_packs_is_counted_as_skipped(self) -> None:
        self._seed(
            candidate_id="dunnes:KKK:222",
            name="Diet Coke 330ml Can",
            catalog_id="ghost-pack",
            raw_record=_dunnes_record("Diet Coke 330ml Can"),
        )
        report = self._classify()
        self.assertEqual(report["counts"]["candidate_cells"]["skipped"], 1)  # type: ignore[index]
        self.assertEqual(report["counts"]["candidate_cells"]["total"], 0)  # type: ignore[index]

    # -- unit-level classifier ----------------------------------------------

    def test_classify_candidate_cell_applies_the_junk_gate(self) -> None:
        facts = type(
            "Facts", (),
            {"name": "POWCUT Hoodie", "attributes": {}, "inference_basis": {},
             "attribute_diffs": {}, "price_status": "valid"},
        )()
        classification, reasons = classify_candidate_cell(
            CATALOG[0], facts, list(CATALOG),  # type: ignore[arg-type]
        )
        self.assertEqual(classification, "excluded")
        self.assertIn("junk gate", reasons[0])

    # -- formatting ---------------------------------------------------------

    def test_format_classification_renders_a_compact_summary(self) -> None:
        self._seed(
            candidate_id="dunnes:LLL:222",
            name="Diet Coke 330ml Can",
            catalog_id="coca-diet-330",
            raw_record=_dunnes_record("Diet Coke 330ml Can"),
        )
        text = format_classification(self._classify())
        self.assertIn("candidate_cells total=1 A=1", text)
        self.assertIn("cells total=1 A=1", text)
        self.assertIn("rerun_targets=0", text)

    def test_report_is_json_serializable(self) -> None:
        self._seed(
            candidate_id="dunnes:MMM:222",
            name="POWCUT Hoodie",
            catalog_id="coca-original-330",
            raw_record=_dunnes_record("POWCUT Hoodie"),
        )
        self.store.set_cell_state(
            "dunnes", "fanta-orange-330", "inconclusive", decided_by="test",
        )
        # Must not raise.
        json.dumps(self._classify())


if __name__ == "__main__":
    unittest.main()
