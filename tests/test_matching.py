import unittest

from beverage_feed.collector import BenchmarkPack
from beverage_feed.matching import SourceListing, match_catalog


class CatalogMatchingTests(unittest.TestCase):
    def setUp(self):
        self.single = BenchmarkPack(
            catalog_id="coke-zero-single",
            name="Coca-Cola Zero Sugar 330ml Can",
            brand="Coca-Cola",
            variant="Zero Sugar",
            pack_count=1,
            unit_size_ml=330,
            package_type="can",
            search_term="Coca-Cola Zero Sugar",
            aliases=("Coke Zero 330ml single",),
        )
        self.six_pack = BenchmarkPack(
            catalog_id="coke-zero-six",
            name="Coca-Cola Zero Sugar 330ml Can x6",
            brand="Coca-Cola",
            variant="Zero Sugar",
            pack_count=6,
            unit_size_ml=330,
            package_type="can",
            search_term="Coca-Cola Zero Sugar 6 pack",
        )

    def test_accepts_alias_and_normalises_litres(self):
        result = match_catalog(
            [self.single, self.six_pack],
            SourceListing(
                retailer="dunnes",
                source_product_reference="sku-1",
                source_item_id="item-1",
                name="Coke Zero 0.33L single can",
                brand="Coca-Cola",
                variant="Zero Sugar",
                package_type="can",
            ),
        )

        self.assertEqual((result.status, result.catalog_id), ("approved", "coke-zero-single"))

    def test_does_not_match_a_single_to_a_multipack(self):
        result = match_catalog(
            [self.single, self.six_pack],
            SourceListing(
                retailer="dunnes",
                source_product_reference="sku-6",
                source_item_id="item-6",
                name="Coca-Cola Zero Sugar 330ml Cans x6",
                brand="Coca-Cola",
                variant="Zero Sugar",
                package_type="can",
            ),
        )

        self.assertEqual((result.status, result.catalog_id), ("approved", "coke-zero-six"))

    def test_routes_equal_candidates_to_review(self):
        duplicate = BenchmarkPack(
            catalog_id="duplicate",
            name="Coca-Cola Zero Sugar 330ml Can duplicate",
            brand="Coca-Cola",
            variant="Zero Sugar",
            pack_count=1,
            unit_size_ml=330,
            package_type="can",
            search_term="Coca-Cola Zero Sugar",
        )
        result = match_catalog(
            [self.single, duplicate],
            SourceListing(
                retailer="dunnes",
                source_product_reference="sku-1",
                source_item_id="item-1",
                name="Coca-Cola Zero Sugar 330ml Can",
                brand="Coca-Cola",
                variant="Zero Sugar",
                package_type="can",
            ),
        )

        self.assertEqual(result.status, "review")
        self.assertIsNone(result.catalog_id)

    def test_unmatched_listing_is_unmapped(self):
        result = match_catalog(
            [self.single],
            SourceListing(
                retailer="dunnes",
                source_product_reference="sku-water",
                source_item_id="item-water",
                name="Ballygowan Still Water 500ml Bottle",
                brand="Ballygowan",
                variant="Still",
                package_type="bottle",
            ),
        )

        self.assertEqual((result.status, result.catalog_id), ("unmapped", None))

    def test_attribute_match_with_a_mismatched_name_is_unmapped(self):
        # Attributes point at the single can, but the source name is a
        # different product: approval requires the name to agree too.
        result = match_catalog(
            [self.single],
            SourceListing(
                retailer="dunnes",
                source_product_reference="sku-1",
                source_item_id="item-1",
                name="Pepsi Max 330ml Can",
                brand="Coca-Cola",
                variant="Zero Sugar",
                unit_size_ml=330,
                pack_count=1,
                package_type="can",
            ),
        )

        self.assertEqual(result.status, "unmapped")
        self.assertIn("does not match", result.reason)

    def test_brand_mismatch_removes_the_catalog_candidate(self):
        result = match_catalog(
            [self.single],
            SourceListing(
                retailer="dunnes",
                source_product_reference="sku-1",
                source_item_id="item-1",
                name="Coca-Cola Zero Sugar 330ml Can",
                brand="Pepsi",
                variant="Zero Sugar",
                unit_size_ml=330,
                pack_count=1,
                package_type="can",
            ),
        )

        self.assertEqual(result.status, "unmapped")
        self.assertIn("no catalog pack", result.reason)

    def test_variant_mismatch_removes_the_catalog_candidate(self):
        result = match_catalog(
            [self.single],
            SourceListing(
                retailer="dunnes",
                source_product_reference="sku-1",
                source_item_id="item-1",
                name="Coca-Cola Original Taste 330ml Can",
                brand="Coca-Cola",
                variant="Original Taste",
                unit_size_ml=330,
                pack_count=1,
                package_type="can",
            ),
        )

        self.assertEqual(result.status, "unmapped")

    def test_package_type_conflict_removes_the_catalog_candidate(self):
        # Source infers a bottle where the catalog only knows a can.
        result = match_catalog(
            [self.single],
            SourceListing(
                retailer="dunnes",
                source_product_reference="sku-1",
                source_item_id="item-1",
                name="Coca-Cola Zero Sugar 330ml Bottle",
                brand="Coca-Cola",
                variant="Zero Sugar",
                unit_size_ml=330,
                pack_count=1,
            ),
        )

        self.assertEqual(result.status, "unmapped")

    def test_carton_is_a_recognised_package_type(self):
        carton = BenchmarkPack(
            catalog_id="coke-zero-carton",
            name="Coca-Cola Zero Sugar 1L Carton",
            brand="Coca-Cola",
            variant="Zero Sugar",
            pack_count=1,
            unit_size_ml=1000,
            package_type="carton",
            search_term="Coca-Cola Zero Sugar 1L",
        )
        result = match_catalog(
            [self.single, carton],
            SourceListing(
                retailer="dunnes",
                source_product_reference="sku-1",
                source_item_id="item-1",
                name="Coca-Cola Zero Sugar 1L Carton",
                brand="Coca-Cola",
                variant="Zero Sugar",
                package_type="carton",
            ),
        )

        self.assertEqual((result.status, result.catalog_id), ("approved", "coke-zero-carton"))


if __name__ == "__main__":
    unittest.main()
