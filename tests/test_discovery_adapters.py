import json
import unittest
from pathlib import Path

from beverage_feed.collector import BenchmarkPack
from beverage_feed.discovery_adapters import (
    AldiDiscoveryAdapter,
    DunnesDiscoveryAdapter,
    LidlDiscoveryAdapter,
    SuperValuDiscoveryAdapter,
    TescoDiscoveryAdapter,
    normalize_listing,
    parse_aldi_category_key,
    parse_aldi_drinks_categories,
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
        self.assertTrue(adapter.capabilities.supports("composite"))
        self.assertFalse(adapter.capabilities.supports("item"))
        self.assertEqual(result.request_counts["search"], 2)

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
        self.assertTrue(adapter.capabilities.supports("product"))
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
        self.assertTrue(adapter.capabilities.supports("tpnb"))

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


class DunnesAliasInclusiveSearchTests(unittest.TestCase):
    """Dunnes' grocery gateway returns different result sets per phrasing, so
    each cell searches its search_term *and* curated aliases — one request per
    unique term, merged and de-duplicated by source identity."""

    ALIASED_PACK = BenchmarkPack(
        catalog_id="coca-original-330-single",
        name="Coca-Cola Original Taste 330ml Can",
        brand="Coca-Cola",
        variant="Original Taste",
        pack_count=1,
        unit_size_ml=330,
        package_type="can",
        search_term="Coca-Cola Original Taste 330ml Can",
        aliases=("Coke Original",),
    )

    @staticmethod
    def payload(reference, name):
        return {"data": {"productSearch": {"products": [{
            "productName": name,
            "productReference": reference,
            "items": [{"itemId": reference, "sellers": [
                {"commertialOffer": {"Price": "1.55"}}]}],
        }]}}}

    def test_searches_the_term_and_each_alias_and_merges_listings(self):
        calls = []

        def client(term):
            calls.append(term)
            if term == "Coke Original":
                # The alias phrasing surfaces the single can the full term misses.
                return self.payload("100298009", "Coke Original 330ml")
            return self.payload("100298010", "Coca-Cola Original Taste 330ml Cans x6")

        result = DunnesDiscoveryAdapter(client).search(self.ALIASED_PACK)

        self.assertEqual(calls, ["Coca-Cola Original Taste 330ml Can", "Coke Original", "Coca-Cola"])
        self.assertEqual(
            sorted(listing.source_identity for listing in result.listings),
            ["100298009:100298009", "100298010:100298010"],
        )
        self.assertEqual(result.request_counts["search"], 3)
        self.assertEqual(adapter_budget(DunnesDiscoveryAdapter), 4)

    def test_duplicate_terms_issue_one_request(self):
        # The catalog's Diet packs use "Diet Coke" as both term and alias.
        pack = BenchmarkPack(
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
        calls = []

        def client(term):
            calls.append(term)
            return self.payload("100298012", "Diet Coke 330ml")

        result = DunnesDiscoveryAdapter(client).search(pack)

        self.assertEqual(calls, ["Diet Coke", "Coca-Cola"])
        self.assertEqual(result.request_counts["search"], 2)
        self.assertEqual([l.source_identity for l in result.listings], ["100298012:100298012"])

    def test_merged_completeness_is_the_conservative_conjunction(self):
        def client(term):
            if term == "Coke Original":
                return {"data": {"productSearch": {"products": []}},
                        "pagination": {"hasMore": True}}
            return {"data": {"productSearch": {"products": []}}}

        result = DunnesDiscoveryAdapter(client).search(self.ALIASED_PACK)

        self.assertIs(result.complete, False)


def adapter_budget(cls):
    return cls.max_requests_per_search


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

        # PACK's search_term and brand are both unique terms (brand-backed search).
        self.assertEqual(calls, [PACK.search_term, PACK.brand])


class AldiDrinksWalkTests(unittest.TestCase):
    """R2 prototype: Aldi Drinks-category walk, dry-run on committed fixtures."""

    def setUp(self) -> None:
        fixture = (
            Path(__file__).resolve().parent
            / "fixtures"
            / "aldi_category_tree_drinks.json"
        )
        self.tree = json.loads(fixture.read_text())
        self.adapter = AldiDiscoveryAdapter(lambda _: {"items": []})

    def test_parses_drinks_subcategories_from_tree_fixture(self) -> None:
        categories = parse_aldi_drinks_categories(self.tree)
        names = [category["name"] for category in categories]
        self.assertEqual(
            names,
            [
                "Tea",
                "Coffee",
                "Hot Chocolate & Malted Drinks",
                "Soft Drinks & Juices",
                "Water",
                "Tonic & Mixers",
            ],
        )
        self.assertEqual(
            [(category["nodeId"], category["url"]) for category in categories][0],
            (82, "/en/drinks/tea"),
        )

    def test_missing_drinks_node_raises(self) -> None:
        with self.assertRaises(ValueError):
            parse_aldi_drinks_categories({"data": []})

    def test_category_key_is_extracted_from_node_payload_fixture(self) -> None:
        node = {
            "data": {
                "type": "category-nodes",
                "id": "85",
                "attributes": {
                    "name": "Soft Drinks & Juices",
                    "categoryKey": "1588161416978079004",
                },
            }
        }
        self.assertEqual(
            parse_aldi_category_key(node), "1588161416978079004"
        )
        with self.assertRaises(ValueError):
            parse_aldi_category_key({"data": {"attributes": {}}})

    def test_walk_lists_each_subcategory_pool_without_live_calls(self) -> None:
        nodes: dict[int, dict] = {}
        keys = {"Soft Drinks & Juices": "1588161416978079004", "Water": "1588161416978079005"}
        for category in parse_aldi_drinks_categories(self.tree):
            key = keys.get(category["name"], "key-unknown")
            nodes[category["nodeId"]] = {
                "data": {"attributes": {"name": category["name"], "categoryKey": key}}
            }
        pools: dict[str, list] = {
            "Soft Drinks & Juices": [
                {"productId": "1001", "name": "Coca-Cola Zero Sugar 330ml Can", "price": "€2.49"},
                {"productId": "1002", "name": "Rockstar Energy Sugar Free 500ml", "price": "€1.99"},
            ],
            "Water": [
                {"productId": "2001", "name": "Vitn Water 1L", "price": "€0.85"},
            ],
        }
        fetch_node = lambda node_id: nodes[node_id]  # noqa: E731
        fetch_category = lambda category: {
            "items": pools.get(category["name"], []),
            "pagination": {"total": len(pools.get(category["name"], [])), "offset": 0},
        }

        results = self.adapter.walk_drinks(self.tree, fetch_node, fetch_category)

        self.assertEqual(len(results), 6)
        by_name = {category["name"]: (category, result) for category, result in results}
        soft, soft_result = by_name["Soft Drinks & Juices"]
        self.assertEqual(soft["categoryKey"], "1588161416978079004")
        self.assertEqual(len(soft_result.listings), 2)
        self.assertEqual(soft_result.complete, True)
        listing = soft_result.listings[0]
        self.assertEqual(listing.source_identity, "1001")
        self.assertEqual(listing.price.raw_value, "€2.49")
        water_result = by_name["Water"][1]
        self.assertEqual(len(water_result.listings), 1)
        # One search request per subcategory, batch size = pool size
        # (floor of 1 for an empty pool, per RequestEvent's positive-size rule).
        self.assertEqual(soft_result.request_counts["search"], 1)
        self.assertEqual(soft_result.batch_sizes["search"], [2])


class LidlDrinksWalkTests(unittest.TestCase):
    """List-only Lidl Drinks category walk (full-feed-coverage step 4): pages
    the category listing into DiscoveryResults without writing any verdicts,
    mappings or store rows. fetch_page is injected — no live HTTP."""

    def setUp(self):
        self.adapter = LidlDiscoveryAdapter(lambda _: {"items": []})

    @staticmethod
    def _page(records, offset, total):
        return {"items": records, "pagination": {"total": total, "offset": offset}}

    @staticmethod
    def _record(product_id):
        return {"productId": product_id, "name": f"Lidl Drink {product_id}", "price": "€1.39"}

    def test_walk_paginates_until_complete_and_returns_listings(self):
        pages = {
            0: self._page([self._record("101"), self._record("102")], 0, 3),
            2: self._page([self._record("103")], 2, 3),
        }
        requested = []

        def fetch_page(offset):
            requested.append(offset)
            return pages[offset]

        results = self.adapter.walk_drinks(fetch_page)

        self.assertEqual(requested, [0, 2])
        self.assertEqual([(offset, result.complete) for offset, result in results],
                         [(0, False), (2, True)])
        all_listings = [listing for _, result in results for listing in result.listings]
        self.assertEqual([listing.source_identity for listing in all_listings],
                         ["101", "102", "103"])
        # One search request per page, batch size = records on that page.
        self.assertEqual([result.request_counts["search"] for _, result in results], [1, 1])
        self.assertEqual(results[0][1].batch_sizes["search"], [2])

    def test_walk_stops_on_an_empty_first_page(self):
        requested = []

        def fetch_page(offset):
            requested.append(offset)
            return self._page([], 0, 0)

        results = self.adapter.walk_drinks(fetch_page)

        self.assertEqual(requested, [0])
        self.assertEqual(len(results), 1)
        self.assertEqual(results[0][1].listings, ())

    def test_walk_returns_normalized_lidl_listings(self):
        """The walk's only output is normalized listing evidence; the no-write
        guarantee is structural (no store seam on the path) and pinned at the
        CLI level (test_walk_drinks_lists_the_pool_without_writing)."""
        def fetch_page(offset):
            return self._page([self._record("101")], 0, 1)

        results = self.adapter.walk_drinks(fetch_page)

        for _, result in results:
            for listing in result.listings:
                self.assertEqual(listing.retailer, "lidl")
                self.assertEqual(listing.price.raw_value, "€1.39")

    def test_walk_without_completion_hits_the_page_bound(self):
        def fetch_page(offset):
            return self._page([self._record("101")], offset, 10**6)

        with self.assertRaises(RuntimeError):
            self.adapter.walk_drinks(lambda offset: fetch_page(offset), max_pages=3)

    def test_max_pages_below_one_is_rejected(self):
        with self.assertRaises(ValueError):
            self.adapter.walk_drinks(lambda offset: self._page([], 0, 0), max_pages=0)
