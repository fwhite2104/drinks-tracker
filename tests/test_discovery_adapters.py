import unittest

from beverage_feed.collector import BenchmarkPack
from beverage_feed.discovery_adapters import (
    DunnesDiscoveryAdapter,
    LidlDiscoveryAdapter,
    SuperValuDiscoveryAdapter,
    TescoDiscoveryAdapter,
    normalize_listing,
)


PACK = BenchmarkPack(
    catalog_id="coke-6",
    name="Coca-Cola Original Taste 330ml Cans x6",
    brand="Coca-Cola",
    variant="Original Taste",
    pack_count=6,
    unit_size_ml=330,
    package_type="can",
    search_term="Coca-Cola Original Taste 330ml Cans x6",
)


class DiscoveryAdapterTests(unittest.TestCase):
    def test_dunnes_normalizes_composite_identity_and_unknown_completeness(self):
        adapter = DunnesDiscoveryAdapter(lambda _: {
            "data": {"productSearch": {"products": [{
                "productName": "Coca-Cola Original Taste 330ml Cans x6",
                "productReference": "ref-6",
                "items": [{"itemId": "item-6", "sellers": [{"commertialOffer": {"Price": "8.40"}}]}],
            }]}}
        })

        result = adapter.search(PACK)

        listing = result.listings[0]
        self.assertEqual(result.complete, "unknown")
        self.assertEqual((listing.source_identity, listing.identity_tier), ("ref-6:item-6", "composite"))
        self.assertEqual(listing.attributes["unit_size_ml"], 330)
        self.assertEqual(listing.attributes["pack_count"], 6)
        self.assertEqual(listing.attributes["total_volume_ml"], 1980)
        self.assertEqual(listing.price.status, "valid")
        self.assertTrue(adapter.supports("composite"))
        self.assertFalse(adapter.supports("item"))
        self.assertEqual(result.request_counts["search"], 1)

    def test_dunnes_reports_explicitly_truncated_results(self):
        adapter = DunnesDiscoveryAdapter(lambda _: {
            "data": {"productSearch": {"products": []}},
            "pagination": {"hasMore": True, "next": "cursor-2"},
        })

        result = adapter.search(PACK)

        self.assertIs(result.complete, False)
        self.assertEqual(result.pagination["next"], "cursor-2")

    def test_supervalu_accounts_for_bootstrap_and_hydrates_by_product_id(self):
        calls = []

        class Client:
            store_id = "store-1"

            def __call__(self, term):
                calls.append(("search", term))
                return {"items": [{
                    "productId": "sv-6",
                    "name": "Coca-Cola Original Taste 330ml Cans x6",
                    "brand": "Coca-Cola",
                    "priceNumeric": "8.40",
                }], "total": 1}

        def hydrate(product_id):
            calls.append(("hydrate", product_id))
            return {"items": [{
                "productId": product_id,
                "name": "Coca-Cola Original Taste 330ml Cans x6",
                "brand": "Coca-Cola",
                "priceNumeric": "8.40",
            }]}

        adapter = SuperValuDiscoveryAdapter(Client(), hydrator=hydrate)
        result = adapter.search(PACK)
        hydrated = adapter.hydrate("sv-6")

        self.assertIs(result.complete, True)
        self.assertEqual(result.request_counts, {"bootstrap": 1, "hydration": 0, "pagination": 0, "search": 1})
        self.assertEqual(hydrated.request_counts["hydration"], 1)
        self.assertEqual(hydrated.listings[0].source_identity, "sv-6")
        self.assertTrue(adapter.supports("product"))
        self.assertEqual(calls, [("search", PACK.search_term), ("hydrate", "sv-6")])

    def test_tesco_separates_search_and_hydration_batch_requests_and_uses_tpnb(self):
        class Client:
            def __call__(self, _term):
                return {"products": [
                    {"tpnb": "tp-1", "title": "Coca-Cola Original Taste 330ml Can", "price": {"actual": "1.40"}},
                    {"tpnb": "tp-2", "title": "Coca-Cola Original Taste 330ml Can", "price": {"actual": "1.40"}},
                ], "pagination": {"hasMore": False}}

            def fetch_product(self, tpnb):
                return {"products": [{"tpnb": tpnb, "title": "Coca-Cola Original Taste 330ml Can"}]}

        adapter = TescoDiscoveryAdapter(Client())
        result = adapter.search(PACK)
        hydrated = adapter.hydrate("tp-1")

        self.assertIs(result.complete, True)
        self.assertEqual(result.request_counts["search"], 1)
        self.assertEqual(result.request_events[1].batch_size, 2)
        self.assertEqual(result.request_events[1].kind, "hydration")
        self.assertEqual(hydrated.listings[0].identity_tier, "tpnb")
        self.assertTrue(adapter.supports("tpnb"))

    def test_normalization_retains_missing_and_conflicting_evidence(self):
        listing = normalize_listing("supervalu", {
            "productId": "sv-1",
            "name": "Coke Zero 0.33L Can x6",
            "brand": "Coca-Cola",
            "unitSizeMl": 500,
            "priceNumeric": "not-a-price",
        }, aliases={"coke": "coca cola"})

        self.assertEqual(listing.attributes["unit_size_ml"], 500)
        self.assertEqual(listing.name_attributes["unit_size_ml"], 330)
        self.assertIn("unit_size_ml", listing.conflicts)
        # The consumer phrasing "Coke Zero" in the name translates to the
        # canonical variant the record never stated (Brand Alias, CONTEXT.md).
        self.assertEqual(listing.attributes["variant"], "zero sugar")
        self.assertEqual(listing.inference_basis["variant"], "brand-alias")
        self.assertEqual(listing.price.status, "malformed")
        self.assertEqual(listing.raw_attributes["name"], "Coke Zero 0.33L Can x6")

    def test_variant_stays_missing_without_any_alias_phrase(self):
        listing = normalize_listing("supervalu", {
            "productId": "sv-2",
            "name": "Fizzy Orange Drink 0.5L Bottle",
            "brand": "Generic",
        })

        self.assertNotIn("variant", listing.attributes)
        self.assertIn("variant", listing.missing_attributes)

    def test_brand_alias_translates_a_brandless_dunnes_record(self):
        # Dunnes gateway records carry no brand/variant fields; the curated
        # Brand Alias dictionary extracts them from the listing name.
        listing = normalize_listing("dunnes", {
            "productName": "Diet Coke 330ml Can",
            "productReference": "100298012",
            "items": [{"itemId": "100298012"}],
        })

        self.assertEqual(listing.attributes["brand"], "coca cola")
        self.assertEqual(listing.attributes["variant"], "diet")
        self.assertEqual(listing.inference_basis["brand"], "brand-alias")

    def test_junk_names_gain_no_brand_identity(self):
        listing = normalize_listing("dunnes", {
            "productName": "POWERCUT Zip Hoodie Navy",
            "productReference": "lamp-1",
            "items": [{"itemId": "lamp-1"}],
        })

        self.assertIsNone(listing.attributes.get("brand"))
        self.assertIsNone(listing.attributes.get("variant"))

    def test_structured_consumer_brand_is_translated_but_the_variant_bar_holds(self):
        # SuperValu lists brand "Diet Coke": the alias rewrites the brand to
        # Coca-Cola; a conflicting stated variant survives untouched so the
        # exact-pack bar still rejects it.
        listing = normalize_listing("supervalu", {
            "productId": "sv-3",
            "name": "Diet Coke Bottle 2L",
            "brand": "Diet Coke",
            "variant": "Zero Sugar",
            "unitSizeMl": 2000,
            "packCount": 1,
            "packageType": "bottle",
        })

        self.assertEqual(listing.attributes["brand"], "coca cola")
        self.assertEqual(listing.inference_basis["brand"], "brand-alias")
        self.assertEqual(listing.attributes["variant"], "zero sugar")

    def test_each_adapter_exposes_complete_truncated_and_unknown_states(self):
        def dunnes(payload):
            return DunnesDiscoveryAdapter(lambda _: payload).search(PACK).complete

        def supervalu(payload):
            return SuperValuDiscoveryAdapter(lambda _: payload).search(PACK).complete

        def tesco(payload):
            return TescoDiscoveryAdapter(lambda _: payload).search(PACK).complete

        payloads = {
            "dunnes": lambda state: {"data": {"productSearch": {"products": []}}, "pagination": {"hasMore": state}},
            "supervalu": lambda state: {"items": [], "pagination": {"hasMore": state}},
            "tesco": lambda state: {"products": [], "pagination": {"hasMore": state}},
        }
        adapters = {"dunnes": dunnes, "supervalu": supervalu, "tesco": tesco}
        for retailer, make_result in adapters.items():
            self.assertIs(make_result(payloads[retailer](False)), True)
            self.assertIs(make_result(payloads[retailer](True)), False)
            unknown = {key: value for key, value in payloads[retailer](False).items() if key != "pagination"}
            self.assertEqual(make_result(unknown), "unknown")

    def test_identity_falls_back_in_the_documented_dunnes_order(self):
        cases = [
            ({"itemId": "item"}, ("item", "item")),
            ({"productReference": "product"}, ("product", "product")),
            # Brand Alias translation rewrites the consumer brand "Coke" to
            # the canonical "coca cola" before the identity signature is
            # built, so fallback identities are catalog-shaped.
            ({}, ("coke 330ml can|coca cola|original|330|1|can", "name_pack_signature")),
        ]
        for fields, expected in cases:
            record = {"productName": "Coke 330ml Can", "brand": "Coke", "variant": "Original", **fields}
            listing = normalize_listing("dunnes", record)
            self.assertEqual((listing.source_identity, listing.identity_tier), expected)

    def test_total_quantity_is_converted_to_per_unit_size(self):
        listing = normalize_listing("tesco", {
            "tpnb": "tp-6",
            "title": "Coca-Cola Original Taste 6 x 330ml Cans",
            "totalVolume": "1.98L",
            "packCount": 6,
        })

        self.assertEqual(listing.attributes["unit_size_ml"], 330)
        self.assertEqual(listing.attributes["total_volume_ml"], 1980)
        self.assertEqual(listing.inference_basis["unit_size_ml"], "name")


if __name__ == "__main__":
    unittest.main()


class CategoryScopeTests(unittest.TestCase):
    """Adapter-level category filters where the retailer API offers them:
    clients expose scoped_search(term, category); the adapter passes its
    declared scope, and plain clients are called exactly as before."""

    def test_lidl_declares_the_verified_drinks_category_scope(self):
        self.assertEqual(LidlDiscoveryAdapter.category_scope, "10071022")

    def test_scoped_client_receives_the_category(self):
        calls = []

        class ScopedClient:
            def scoped_search(self, term, category):
                calls.append((term, category))
                return {"items": []}

        LidlDiscoveryAdapter(ScopedClient()).search(PACK)

        self.assertEqual(calls, [(PACK.search_term, "10071022")])

    def test_plain_client_is_called_with_the_term_only(self):
        calls = []

        def client(term):
            calls.append(term)
            return {"items": []}

        LidlDiscoveryAdapter(client).search(PACK)

        self.assertEqual(calls, [PACK.search_term])

    def test_adapters_without_a_scope_ignore_a_scoped_client(self):
        calls = []

        class ScopedClient:
            def scoped_search(self, term, category):
                raise AssertionError("must not be used without a category scope")

            def __call__(self, term):
                calls.append(term)
                return {"data": {"productSearch": {"products": []}}}

        DunnesDiscoveryAdapter(ScopedClient()).search(PACK)

        self.assertEqual(calls, [PACK.search_term])
