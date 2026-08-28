import unittest
from pathlib import Path

from beverage_feed.collector import BenchmarkPack
from beverage_feed.matching import (
    SourceListing,
    attribute_candidates,
    brand_matches_alias,
    is_relevant_candidate,
    load_brand_aliases,
    match_catalog,
    resolve_brand_alias,
    search_formulations,
)


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


class BrandAliasTests(unittest.TestCase):
    """Brand Alias (CONTEXT.md): curated aliases translate a retailer's
    consumer brand name to the canonical brand/variant before the exact-pack
    bar is applied; the bar itself (variant, pack count, unit size) never
    weakens."""

    def setUp(self):
        self.diet_two_litre = BenchmarkPack(
            catalog_id="coca-diet-2000",
            name="Coca-Cola Diet 2L Bottle",
            brand="Coca-Cola",
            variant="Diet",
            pack_count=1,
            unit_size_ml=2000,
            package_type="bottle",
            search_term="Diet Coke",
            aliases=("Diet Coke",),
        )
        self.zero_two_litre = BenchmarkPack(
            catalog_id="coca-zero-2000",
            name="Coca-Cola Zero Sugar 2L Bottle",
            brand="Coca-Cola",
            variant="Zero Sugar",
            pack_count=1,
            unit_size_ml=2000,
            package_type="bottle",
            search_term="Coca-Cola Zero Sugar",
            aliases=("Coke Zero",),
        )

    def listing(self, **overrides):
        fields = dict(
            retailer="supervalu",
            source_product_reference="1023917003",
            source_item_id="1023917003",
            name="Diet Coke Bottle 2L",
            brand="Diet Coke",
            variant="Diet",
            unit_size_ml=2000,
            pack_count=1,
            package_type="bottle",
        )
        fields.update(overrides)
        return SourceListing(**fields)

    def test_consumer_brand_alias_matches_catalog_brand(self):
        # SuperValu lists "Diet Coke" under the consumer brand name while the
        # catalog records brand=Coca-Cola, variant=Diet.
        result = match_catalog([self.diet_two_litre], self.listing())

        self.assertEqual((result.status, result.catalog_id), ("approved", "coca-diet-2000"))

    def test_consumer_brand_alias_matches_regardless_of_token_order(self):
        result = match_catalog(
            [self.diet_two_litre], self.listing(brand="Coke Diet", name="Coke Diet Bottle 2L"),
        )

        self.assertEqual((result.status, result.catalog_id), ("approved", "coca-diet-2000"))

    def test_brand_alias_does_not_widen_to_other_variants(self):
        # "Diet Coke" is a curated alias of the Diet pack only; it must never
        # approve the Zero Sugar pack.
        result = match_catalog([self.zero_two_litre], self.listing())

        self.assertEqual((result.status, result.catalog_id), ("unmapped", None))

    def test_brand_alias_never_weakens_the_variant_bar(self):
        # The alias translates the brand; the variant must still agree exactly.
        result = match_catalog(
            [self.diet_two_litre], self.listing(variant="Zero Sugar"),
        )

        self.assertEqual((result.status, result.catalog_id), ("unmapped", None))

    def test_cross_variant_pack_alias_does_not_match_the_brand(self):
        # A mis-curated cross-variant pack alias ("Diet Coke" on the Zero
        # Sugar pack) can never bridge the brand check: translating through
        # an alias must agree with the pack's canonical brand and variant.
        zero_pack = BenchmarkPack(
            catalog_id="coca-zero-2000-bad-alias",
            name="Coca-Cola Zero Sugar 2L Bottle",
            brand="Coca-Cola",
            variant="Zero Sugar",
            pack_count=1,
            unit_size_ml=2000,
            package_type="bottle",
            search_term="Coca-Cola Zero Sugar",
            aliases=("Diet Coke",),  # curated error: a Diet alias on a Zero pack
        )

        self.assertFalse(brand_matches_alias(zero_pack, "Diet Coke"))


class CuratedBrandAliasDictionaryTests(unittest.TestCase):
    """The curated Brand Alias dictionary translates consumer phrasings into
    canonical catalog identity (CONTEXT.md: Brand Alias). It is a code-level
    curated table so review sprints extend it by reviewable edits; nothing
    auto-applies at runtime."""

    def test_diet_coke_resolves_to_coca_cola_diet(self):
        translation = resolve_brand_alias("Diet Coke")

        self.assertEqual((translation.brand, translation.variant), ("Coca-Cola", "Diet"))

    def test_resolves_a_phrase_inside_a_longer_text(self):
        translation = resolve_brand_alias("Coke Zero 0.33L Can")

        self.assertEqual((translation.brand, translation.variant), ("Coca-Cola", "Zero Sugar"))

    def test_bare_consumer_brand_resolves_with_unknown_variant(self):
        translation = resolve_brand_alias("Coke")

        self.assertEqual((translation.brand, translation.variant), ("Coca-Cola", None))

    def test_longest_phrase_wins(self):
        translation = resolve_brand_alias("Coca-Cola Zero Sugar 330ml")

        self.assertEqual(translation.phrase, "coca cola zero")
        self.assertEqual(translation.variant, "Zero Sugar")

    def test_matching_is_case_and_hyphen_insensitive(self):
        self.assertEqual(resolve_brand_alias("DIET-COKE").variant, "Diet")

    def test_junk_names_resolve_to_nothing(self):
        for junk in ("POWERCUT Zip Hoodie", "LED Desk Lamp 5W", "Cola Sweets Bag", "", None):
            self.assertIsNone(resolve_brand_alias(junk))

    def test_dictionary_loads_from_the_data_file(self):
        # CONTRIBUTING §10: curated inputs live in data/ files; the alias
        # dictionary is loaded from data/brand_aliases.json like the catalog.
        table = load_brand_aliases(Path("data") / "brand_aliases.json")

        self.assertEqual(table.get("diet coke"), ("Coca-Cola", "Diet"))
        self.assertEqual(table.get("coke zero"), ("Coca-Cola", "Zero Sugar"))
        self.assertEqual(table.get("coke"), ("Coca-Cola", None))

    def test_missing_dictionary_file_falls_back_to_empty(self):
        # Consistent with mappings.json handling: a missing file degrades to
        # an empty table instead of raising.
        self.assertEqual(load_brand_aliases(Path("no") / "such" / "aliases.json"), {})

    def test_dictionary_covers_the_catalog_top_brands(self):
        table = load_brand_aliases(Path("data") / "brand_aliases.json")
        covered = {brand for brand, _ in table.values()}
        for brand in ("Coca-Cola", "7UP", "Fanta", "Sprite", "Lucozade", "Rockstar"):
            self.assertIn(brand, covered)


class CuratedDictionaryBrandAliasTests(unittest.TestCase):
    """brand_matches_alias is fed by both curated sources: the pack's own
    aliases and the shared Brand Alias dictionary; the exact-pack bar is
    unchanged either way."""

    def setUp(self):
        # Mirrors the real catalog entry, whose aliases bridge the consumer
        # name for the name check; only the shared dictionary can bridge the
        # brand *attribute* translation.
        self.diet_can = BenchmarkPack(
            catalog_id="coca-diet-330-single",
            name="Coca-Cola Diet 330ml Can",
            brand="Coca-Cola",
            variant="Diet",
            pack_count=1,
            unit_size_ml=330,
            package_type="can",
            search_term="Diet Coke",
            aliases=("Diet Coke",),
        )

    def test_dictionary_alias_matches_without_pack_aliases(self):
        self.assertTrue(brand_matches_alias(self.diet_can, "Diet Coke"))

    def test_dictionary_alias_translates_before_the_exact_bar(self):
        result = match_catalog(
            [self.diet_can],
            SourceListing(
                retailer="dunnes",
                source_product_reference="100298012",
                source_item_id="100298012",
                name="Diet Coke 330ml Can",
                brand="Diet Coke",
                variant="Diet",
                unit_size_ml=330,
                pack_count=1,
                package_type="can",
            ),
        )

        self.assertEqual((result.status, result.catalog_id), ("approved", "coca-diet-330-single"))

    def test_dictionary_alias_never_widens_to_another_variant(self):
        # The Diet pack's alias "Diet Coke" must not approve a Zero Sugar
        # listing whose only bridge is the same dictionary family.
        result = match_catalog(
            [self.diet_can],
            SourceListing(
                retailer="dunnes",
                source_product_reference="100298099",
                source_item_id="100298099",
                name="Coke Zero 330ml Can",
                brand="Coke Zero",
                variant="Zero Sugar",
                unit_size_ml=330,
                pack_count=1,
                package_type="can",
            ),
        )

        self.assertEqual((result.status, result.catalog_id), ("unmapped", None))

    def test_dictionary_alias_never_weakens_the_variant_bar(self):
        self.assertFalse(brand_matches_alias(self.diet_can, "Coke Zero"))

    def test_plain_brand_listing_without_variant_never_matches_by_size_alone(self):
        # A plain "Coca-Cola" listing carrying no variant evidence must not
        # match a Zero Sugar pack just because size and count agree; a
        # missing variant can never silently widen the match.
        zero_can = BenchmarkPack(
            catalog_id="coca-zero-330-single",
            name="Coca-Cola Zero Sugar 330ml Can",
            brand="Coca-Cola",
            variant="Zero Sugar",
            pack_count=1,
            unit_size_ml=330,
            package_type="can",
            search_term="Coca-Cola Zero Sugar",
        )
        listing = SourceListing(
            retailer="dunnes",
            source_product_reference="sku-plain",
            source_item_id="item-plain",
            name="Coca-Cola 330ml Can",
            brand="Coca-Cola",
            unit_size_ml=330,
            pack_count=1,
            package_type="can",
        )

        self.assertEqual(attribute_candidates([zero_can], listing), [])


class JunkGateTests(unittest.TestCase):
    """Universal relevance gate: a candidate attaches to a cell only when its
    name shares at least one brand/identity token with the search term or
    pack. POWERCUT/LED-lamp-class junk stops attaching to drink cells."""

    def setUp(self):
        self.diet_can = BenchmarkPack(
            catalog_id="coca-diet-330-single",
            name="Coca-Cola Diet 330ml Can",
            brand="Coca-Cola",
            variant="Diet",
            pack_count=1,
            unit_size_ml=330,
            package_type="can",
            search_term="Diet Coke",
            aliases=("Diet Coke",),
        )

    def test_diet_coke_listing_shares_identity_with_the_pack(self):
        self.assertTrue(is_relevant_candidate("Diet Coke 330ml Can", self.diet_can))

    def test_pack_name_tokens_are_identity_tokens(self):
        self.assertTrue(is_relevant_candidate("Coca-Cola Diet Can 330ml", self.diet_can))

    def test_powercut_and_lamp_class_names_never_attach(self):
        for junk in ("POWERCUT Zip Hoodie Navy", "LED Desk Lamp 5W", "Batteries AA 4 Pack"):
            self.assertFalse(is_relevant_candidate(junk, self.diet_can))

    def test_size_tokens_alone_are_not_identity(self):
        # A lamp quoting "330ml" still shares no brand/identity token.
        self.assertFalse(is_relevant_candidate("LED Lamp 330ml", self.diet_can))

    def test_alias_phrasing_shares_identity(self):
        zero_pack = BenchmarkPack(
            catalog_id="coca-zero-330-6",
            name="Coca-Cola Zero Sugar 330ml Cans x6",
            brand="Coca-Cola",
            variant="Zero Sugar",
            pack_count=6,
            unit_size_ml=330,
            package_type="can",
            search_term="Coca-Cola Zero Sugar 330ml Cans x6",
            aliases=("Coke Zero",),
        )

        self.assertTrue(is_relevant_candidate("Coke Zero 6 Pack", zero_pack))


class SearchFormulationTests(unittest.TestCase):
    """Ticket 14 term expansion: alternate search formulations per pack.

    Count-explicit ("Coke Zero 8 pack"), alias-explicit ("Diet Coke"), and
    size-explicit ("Coca-Cola Zero Sugar 330ml") phrasings, on top of the
    pack's own search term and curated aliases.
    """

    def multipack(self) -> BenchmarkPack:
        return BenchmarkPack(
            catalog_id="coke-zero-8x330",
            name="Coca-Cola Zero Sugar 330ml Can x8",
            brand="Coca-Cola",
            variant="Zero Sugar",
            pack_count=8,
            unit_size_ml=330,
            package_type="can",
            search_term="Coca-Cola Zero Sugar",
            aliases=("Coke Zero",),
        )

    def test_covers_base_alias_count_and_size_formulations(self):
        terms = list(search_formulations(self.multipack()))

        self.assertEqual(terms[0], "Coca-Cola Zero Sugar")
        self.assertIn("Coke Zero", terms)  # alias-explicit
        self.assertIn("Coca-Cola Zero Sugar 8 pack", terms)  # count-explicit
        self.assertIn("Coke Zero 8 pack", terms)  # alias + count-explicit
        self.assertIn("Coca-Cola Zero Sugar 330ml", terms)  # size-explicit
        self.assertIn("Coke Zero 330ml", terms)  # alias + size-explicit

    def test_single_packs_get_no_count_formulation(self):
        single = BenchmarkPack(
            catalog_id="coke-zero-330",
            name="Coca-Cola Zero Sugar 330ml Can",
            brand="Coca-Cola",
            variant="Zero Sugar",
            pack_count=1,
            unit_size_ml=330,
            package_type="can",
            search_term="Coca-Cola Zero Sugar",
        )

        self.assertNotIn("Coca-Cola Zero Sugar 1 pack", search_formulations(single))

    def test_whole_litres_are_explicit(self):
        bottle = BenchmarkPack(
            catalog_id="coke-diet-2000",
            name="Coca-Cola Diet 2L Bottle",
            brand="Coca-Cola",
            variant="Diet",
            pack_count=1,
            unit_size_ml=2000,
            package_type="bottle",
            search_term="Diet Coke 2L",
        )

        self.assertIn("Coca-Cola Diet 2 litre", search_formulations(bottle))

    def test_formulations_are_unique_and_order_preserving(self):
        terms = search_formulations(self.multipack())

        self.assertEqual(len(terms), len(set(terms)))
