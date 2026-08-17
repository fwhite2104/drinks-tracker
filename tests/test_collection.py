import io
import json
import sqlite3
import tempfile
from contextlib import closing, redirect_stdout
import unittest
from pathlib import Path
from unittest.mock import patch

from beverage_feed.collector import (
    BenchmarkPack,
    DunnesMapping,
    SuperValuClient,
    SuperValuMapping,
    TescoClient,
    TescoMapping,
    collect_catalog,
    collect_one,
    collect_supervalu_one,
    collect_tesco_one,
    collect_run,
    current_feed,
    last_seen,
    main,
    price_history,
    purge_retention,
)


class CollectionCommandTests(unittest.TestCase):
    def setUp(self):
        self.pack = BenchmarkPack(
            catalog_id="coke-zero-330-single",
            name="Coca-Cola Zero Sugar 330ml Can",
            brand="Coca-Cola",
            variant="Zero Sugar",
            pack_count=1,
            unit_size_ml=330,
            package_type="can",
            search_term="Coca-Cola Zero Sugar 330ml",
        )
        self.mapping = DunnesMapping(
            catalog_id=self.pack.catalog_id,
            expected_product_name="Coca-Cola Zero Sugar 330ml",
            source_product_reference="COKE-ZERO-330",
            source_item_id="COKE-ZERO-330-EA",
        )

    def test_collects_and_persists_one_observation(self):
        payload = {
            "data": {
                "productSearch": {
                    "products": [
                        {
                            "productName": "Coca-Cola Zero Sugar 330ml",
                            "productReference": "COKE-ZERO-330",
                            "items": [
                                {
                                    "itemId": "COKE-ZERO-330-EA",
                                    "sellers": [
                                        {
                                            "commertialOffer": {
                                                "Price": "2.49",
                                                "ListPrice": "2.99",
                                            }
                                        }
                                    ],
                                }
                            ],
                        }
                    ]
                }
            }
        }

        with tempfile.TemporaryDirectory() as directory:
            database = Path(directory) / "feed.sqlite"
            summary = collect_one(self.pack, self.mapping, lambda _: payload, database)

            self.assertEqual(summary["status"], "observed")
            self.assertEqual(summary["observed_count"], 1)
            with closing(sqlite3.connect(database)) as connection:
                observation = connection.execute(
                    """
                    SELECT catalog_id, retailer, source_item_id, displayed_price,
                           currency, component_unit_price, price_per_litre
                    FROM price_observations
                    """
                ).fetchone()
                result = connection.execute(
                    "SELECT status FROM collection_results"
                ).fetchone()
                run = connection.execute(
                    "SELECT status, observed_count FROM collection_runs"
                ).fetchone()

            self.assertEqual(
                observation,
                (
                    "coke-zero-330-single",
                    "dunnes",
                    "COKE-ZERO-330-EA",
                    "2.49",
                    "EUR",
                    "2.49",
                    "7.5455",
                ),
            )
            self.assertEqual(result, ("observed",))
            self.assertEqual(run, ("completed", 1))

    def test_catalog_run_only_collects_approved_mappings(self):
        second = BenchmarkPack(
            catalog_id="unapproved-pack",
            name="Other Drink 330ml Can",
            brand="Other",
            variant="Original",
            pack_count=1,
            unit_size_ml=330,
            package_type="can",
            search_term="Other Drink",
        )
        review_mapping = DunnesMapping(
            catalog_id=second.catalog_id,
            expected_product_name="Other Drink 330ml",
            status="review",
        )

        with tempfile.TemporaryDirectory() as directory:
            summaries = collect_catalog(
                [self.pack, second],
                [self.mapping, review_mapping],
                lambda _: {
                    "data": {
                        "productSearch": {
                            "products": [
                                {
                                    "productName": "Coca-Cola Zero Sugar 330ml",
                                    "productReference": "COKE-ZERO-330",
                                    "items": [
                                        {
                                            "itemId": "COKE-ZERO-330-EA",
                                            "sellers": [{"commertialOffer": {"Price": "2.49"}}],
                                        }
                                    ],
                                }
                            ]
                        }
                    }
                },
                Path(directory) / "feed.sqlite",
            )

        self.assertEqual(len(summaries), 1)
        self.assertEqual(summaries[0]["catalog_id"], self.pack.catalog_id)

    def test_unapproved_mapping_cannot_create_an_observation(self):
        calls = []
        mapping = DunnesMapping(
            catalog_id=self.pack.catalog_id,
            expected_product_name=self.mapping.expected_product_name,
            status="review",
        )

        with tempfile.TemporaryDirectory() as directory:
            database = Path(directory) / "feed.sqlite"
            summary = collect_one(
                self.pack,
                mapping,
                lambda term: calls.append(term),
                database,
            )

            self.assertEqual(summary["status"], "unmapped")
            self.assertEqual(calls, [])
            with closing(sqlite3.connect(database)) as connection:
                self.assertEqual(
                    connection.execute("SELECT COUNT(*) FROM price_observations").fetchone()[0],
                    0,
                )

    def test_unmatched_source_product_is_saved_as_candidate(self):
        payload = {
            "data": {
                "productSearch": {
                    "products": [
                        {
                            "productName": "Mystery Cola 330ml",
                            "productReference": "MYSTERY-330",
                            "items": [
                                {
                                    "itemId": "MYSTERY-330-EA",
                                    "sellers": [{"commertialOffer": {"Price": "1.20"}}],
                                }
                            ],
                        }
                    ]
                }
            }
        }

        with tempfile.TemporaryDirectory() as directory:
            database = Path(directory) / "feed.sqlite"
            summary = collect_one(self.pack, self.mapping, lambda _: payload, database)

            self.assertEqual(summary["status"], "not_found")
            with closing(sqlite3.connect(database)) as connection:
                candidate = connection.execute(
                    """
                    SELECT retailer, source_product_reference, source_item_id,
                           source_product_name, displayed_price, status
                    FROM catalog_candidates
                    """
                ).fetchone()

            self.assertEqual(
                candidate,
                ("dunnes", "MYSTERY-330", "MYSTERY-330-EA", "Mystery Cola 330ml", "1.20", "pending_review"),
            )

    def test_tesco_client_reads_api_key_from_environment(self):
        with patch.dict("os.environ", {"TESCO_API_KEY": "environment-key"}):
            client = TescoClient()

        self.assertEqual(client.api_key, "environment-key")

    def test_tesco_client_hydrates_only_requested_tpnb(self):
        class Response(io.BytesIO):
            status = 200

            def __enter__(self):
                return self

            def __exit__(self, *args):
                self.close()

        response = Response(json.dumps([{
            "data": {"product": {
                "id": "tesco-id",
                "title": "Coca-Cola Zero Sugar 330ml Can",
                "price": {"actual": 2.49},
            }}
        }]).encode())
        requests = []

        class Opener:
            def open(self, request, timeout):
                requests.append(request)
                return response

        client = TescoClient(api_key="test-key", opener=Opener(), min_request_interval=0)
        payload = client.fetch_product("12345")

        self.assertEqual(payload["products"][0]["tpnb"], "12345")
        body = json.loads(requests[0].data)
        self.assertEqual(len(body), 1)
        self.assertEqual(body[0]["variables"], {"tpnb": "12345"})

    def test_tesco_client_searches_and_hydrates_product_details(self):
        class Response(io.BytesIO):
            status = 200

            def __enter__(self):
                return self

            def __exit__(self, *args):
                self.close()

        responses = [
            Response(json.dumps({
                "ie": {"ghs": {"products": {"results": [{"tpnb": 12345}]}}}
            }).encode()),
            Response(json.dumps([{
                "data": {"product": {
                    "id": "tesco-id",
                    "title": "Coca-Cola Zero Sugar 330ml Can",
                    "price": {"actual": 2.49},
                }}
            }]).encode()),
        ]
        requests = []

        class Opener:
            def open(self, request, timeout):
                requests.append(request)
                return responses.pop(0)

        client = TescoClient(api_key="test-key", opener=Opener())
        payload = client("Coca-Cola")

        self.assertEqual(payload["products"][0]["tpnb"], "12345")
        self.assertEqual(payload["products"][0]["price"]["actual"], 2.49)
        self.assertEqual(requests[0].get_method(), "GET")
        self.assertIn("search.api.tesco.com/search?", requests[0].full_url)
        self.assertEqual(requests[1].get_method(), "POST")
        self.assertEqual(requests[1].full_url, "https://xapi.tesco.com/")
        self.assertEqual(requests[1].get_header("X-apikey"), "test-key")

    def test_collects_tesco_price_clubcard_and_deposit(self):
        mapping = TescoMapping(
            catalog_id=self.pack.catalog_id,
            expected_product_name="Coca-Cola Zero Sugar 330ml Can",
            source_tpnb="12345",
        )
        payload = {
            "products": [{
                "tpnb": "12345",
                "id": "tesco-id",
                "gtin": "0500000000000",
                "title": "Coca-Cola Zero Sugar 330ml Can",
                "price": {"actual": "2.49"},
                "promotions": [{"description": "€1.99 Clubcard Price"}],
                "details": {"taxDetails": [{"groupName": "Deposit", "amount": "€0.15"}]},
            }]
        }

        with tempfile.TemporaryDirectory() as directory:
            database = Path(directory) / "feed.sqlite"
            summary = collect_tesco_one(
                self.pack, mapping, lambda _: payload, database
            )
            self.assertEqual(summary["status"], "observed")
            with closing(sqlite3.connect(database)) as connection:
                observation = connection.execute(
                    """
                    SELECT retailer, source_product_reference, source_item_id,
                           displayed_price, clubcard_price, drs_deposit, source_scope
                    FROM price_observations
                    """
                ).fetchone()

        self.assertEqual(
            observation,
            ("tesco", "12345", "tesco-id", "2.49", "1.99", "0.15", None),
        )

    def test_european_decimal_price_is_parsed_exactly(self):
        mapping = TescoMapping(
            catalog_id=self.pack.catalog_id,
            expected_product_name="Coca-Cola Zero Sugar 330ml Can",
            source_tpnb="12345",
        )
        payload = {
            "products": [{
                "tpnb": "12345",
                "id": "tesco-id",
                "title": "Coca-Cola Zero Sugar 330ml Can",
                "price": {"actual": "€1,99"},
            }]
        }

        with tempfile.TemporaryDirectory() as directory:
            database = Path(directory) / "feed.sqlite"
            summary = collect_tesco_one(
                self.pack, mapping, lambda _: payload, database
            )
            with closing(sqlite3.connect(database)) as connection:
                price = connection.execute(
                    "SELECT displayed_price FROM price_observations"
                ).fetchone()[0]

        self.assertEqual(summary["status"], "observed")
        self.assertEqual(price, "1.99")

    def test_tesco_collection_uses_direct_tpnb_fetcher(self):
        mapping = TescoMapping(
            catalog_id=self.pack.catalog_id,
            expected_product_name="Coca-Cola Zero Sugar 330ml Can",
            source_tpnb="12345",
        )
        payload = {
            "products": [{
                "tpnb": "12345",
                "id": "tesco-id",
                "title": "Coca-Cola Zero Sugar 330ml Can",
                "price": {"actual": "2.49"},
            }]
        }
        calls = []

        class DirectFetcher:
            def __call__(self, _search_term):
                raise AssertionError("search fetch should not be used for a known TPNB")

            def fetch_product(self, tpnb):
                calls.append(tpnb)
                return payload

        with tempfile.TemporaryDirectory() as directory:
            summary = collect_tesco_one(
                self.pack, mapping, DirectFetcher(), Path(directory) / "feed.sqlite"
            )

        self.assertEqual(summary["status"], "observed")
        self.assertEqual(calls, ["12345"])

    def test_tesco_search_fallback_from_expected_direct_hydration_records_diagnostic(self):
        mapping = TescoMapping(
            catalog_id=self.pack.catalog_id,
            expected_product_name="Coca-Cola Zero Sugar 330ml Can",
            source_tpnb="12345",
        )
        payload = {
            "products": [{
                "tpnb": "12345",
                "id": "tesco-id",
                "title": "Coca-Cola Zero Sugar 330ml Can",
                "price": {"actual": "2.49"},
            }]
        }
        with tempfile.TemporaryDirectory() as directory:
            database = Path(directory) / "feed.sqlite"
            summary = collect_tesco_one(
                self.pack, mapping, lambda _: payload, database
            )
            with sqlite3.connect(database) as connection:
                events = connection.execute(
                    "SELECT event, level FROM collection_diagnostics"
                ).fetchall()

        self.assertEqual(summary["status"], "observed")
        self.assertIn(("collection_fallback", "warning"), events)

    def test_tesco_no_match_is_not_found(self):
        mapping = TescoMapping(
            catalog_id=self.pack.catalog_id,
            expected_product_name="Coca-Cola Zero Sugar 330ml Can",
        )
        with tempfile.TemporaryDirectory() as directory:
            summary = collect_tesco_one(
                self.pack, mapping, lambda _: {"products": []},
                Path(directory) / "feed.sqlite"
            )
        self.assertEqual(summary["status"], "not_found")

    def test_tesco_ignores_meal_deal_clubcard_promotions(self):
        mapping = TescoMapping(
            catalog_id=self.pack.catalog_id,
            expected_product_name="Coca-Cola Zero Sugar 330ml Can",
            source_tpnb="12345",
        )
        payload = {
            "products": [{
                "tpnb": "12345",
                "id": "tesco-id",
                "title": "Coca-Cola Zero Sugar 330ml Can",
                "price": {"actual": "2.49"},
                "promotions": [{
                    "description": "€4.75 Meal Deal Drink Clubcard Price"
                }],
            }]
        }

        with tempfile.TemporaryDirectory() as directory:
            database = Path(directory) / "feed.sqlite"
            collect_tesco_one(self.pack, mapping, lambda _: payload, database)
            with closing(sqlite3.connect(database)) as connection:
                clubcard_price = connection.execute(
                    "SELECT clubcard_price FROM price_observations"
                ).fetchone()[0]

        self.assertIsNone(clubcard_price)

    def test_tesco_ignores_multi_buy_clubcard_promotions(self):
        mapping = TescoMapping(
            catalog_id=self.pack.catalog_id,
            expected_product_name="Coca-Cola Zero Sugar 330ml Can",
            source_tpnb="12345",
        )
        payload = {
            "products": [{
                "tpnb": "12345",
                "id": "tesco-id",
                "title": "Coca-Cola Zero Sugar 330ml Can",
                "price": {"actual": "2.49"},
                "promotions": [{
                    "description": "Any 2 for €3.50 Clubcard Price"
                }],
            }]
        }

        with tempfile.TemporaryDirectory() as directory:
            database = Path(directory) / "feed.sqlite"
            collect_tesco_one(self.pack, mapping, lambda _: payload, database)
            with closing(sqlite3.connect(database)) as connection:
                clubcard_price = connection.execute(
                    "SELECT clubcard_price FROM price_observations"
                ).fetchone()[0]

        self.assertIsNone(clubcard_price)

    def test_tesco_malformed_price_is_a_source_error(self):
        mapping = TescoMapping(
            catalog_id=self.pack.catalog_id,
            expected_product_name="Coca-Cola Zero Sugar 330ml Can",
        )
        payload = {
            "products": [{
                "tpnb": "12345",
                "id": "tesco-id",
                "title": "Coca-Cola Zero Sugar 330ml Can",
                "price": {"actual": "not-a-price"},
            }]
        }
        with tempfile.TemporaryDirectory() as directory:
            summary = collect_tesco_one(
                self.pack, mapping, lambda _: payload,
                Path(directory) / "feed.sqlite"
            )
        self.assertEqual(summary["status"], "source_error")

    def test_tesco_source_failure_is_recorded_without_an_observation(self):
        mapping = TescoMapping(
            catalog_id=self.pack.catalog_id,
            expected_product_name="Coca-Cola Zero Sugar 330ml Can",
        )
        with tempfile.TemporaryDirectory() as directory:
            database = Path(directory) / "feed.sqlite"
            summary = collect_tesco_one(
                self.pack, mapping,
                lambda _: (_ for _ in ()).throw(RuntimeError("Tesco HTTP 503")),
                database,
            )
            self.assertEqual(summary["status"], "source_error")
            with closing(sqlite3.connect(database)) as connection:
                self.assertEqual(
                    connection.execute("SELECT status, error FROM collection_results").fetchone(),
                    ("source_error", "Tesco HTTP 503"),
                )

    def test_supervalu_client_initialises_cookie_and_uses_configured_store_route(self):
        class Response(io.BytesIO):
            status = 200

            def __enter__(self):
                return self

            def __exit__(self, *args):
                self.close()

        responses = [
            Response(b"<html></html>"),
            Response(json.dumps({"items": []}).encode()),
        ]
        urls = []

        class Opener:
            def open(self, request, timeout):
                urls.append(request.full_url)
                return responses.pop(0)

        client = SuperValuClient("store 123", opener=Opener())
        self.assertEqual(client("Coca-Cola"), {"items": []})
        self.assertEqual(
            urls,
            [
                "https://shop.supervalu.ie/",
                "https://storefrontgateway.supervalu.ie/api/stores/store%20123/search?q=Coca-Cola&take=50",
            ],
        )

    def test_collects_supervalu_price_scope_deposit_and_loyalty_price(self):
        mapping = SuperValuMapping(
            catalog_id=self.pack.catalog_id,
            expected_product_name="Coca-Cola Zero Sugar Can (330 ml)",
            source_product_id="SV-330",
        )
        payload = {
            "count": 1,
            "items": [
                {
                    "productId": "SV-330",
                    "sku": "SV-330-SKU",
                    "name": "Coca-Cola Zero Sugar Can (330 ml)",
                    "priceNumeric": 2.49,
                    "clubcardPrice": "1.99",
                    "taxDetails": [{"groupName": "Deposit", "amount": "€0.15"}],
                }
            ],
        }

        with tempfile.TemporaryDirectory() as directory:
            database = Path(directory) / "feed.sqlite"
            summary = collect_supervalu_one(
                self.pack,
                mapping,
                lambda _: payload,
                database,
                store_id="store-123",
            )

            self.assertEqual(summary["status"], "observed")
            self.assertEqual(summary["retailer"], "supervalu")
            with closing(sqlite3.connect(database)) as connection:
                observation = connection.execute(
                    """
                    SELECT retailer, source_item_id, displayed_price, clubcard_price,
                           drs_deposit, source_scope, component_unit_price, price_per_litre
                    FROM price_observations
                    """
                ).fetchone()
                result = connection.execute(
                    "SELECT status, source_scope FROM collection_results"
                ).fetchone()

        self.assertEqual(
            observation,
            ("supervalu", "SV-330-SKU", "2.49", "1.99", "0.15", "store-123", "2.49", "7.5455"),
        )
        self.assertEqual(result, ("observed", "store-123"))

    def test_supervalu_no_match_is_not_found(self):
        mapping = SuperValuMapping(
            catalog_id=self.pack.catalog_id,
            expected_product_name="Coca-Cola Zero Sugar Can (330 ml)",
        )

        with tempfile.TemporaryDirectory() as directory:
            summary = collect_supervalu_one(
                self.pack,
                mapping,
                lambda _: {"count": 0, "items": []},
                Path(directory) / "feed.sqlite",
                store_id="store-123",
            )

        self.assertEqual(summary["status"], "not_found")
        self.assertEqual(summary["failed_count"], 0)

    def test_supervalu_malformed_price_is_a_source_error(self):
        mapping = SuperValuMapping(
            catalog_id=self.pack.catalog_id,
            expected_product_name="Coca-Cola Zero Sugar Can (330 ml)",
        )
        payload = {
            "items": [
                {
                    "productId": "SV-330",
                    "sku": "SV-330-SKU",
                    "name": "Coca-Cola Zero Sugar Can (330 ml)",
                    "priceNumeric": "not-a-price",
                }
            ]
        }

        with tempfile.TemporaryDirectory() as directory:
            database = Path(directory) / "feed.sqlite"
            summary = collect_supervalu_one(
                self.pack, mapping, lambda _: payload, database, store_id="store-123"
            )

            self.assertEqual(summary["status"], "source_error")
            with closing(sqlite3.connect(database)) as connection:
                self.assertEqual(
                    connection.execute(
                        "SELECT status FROM collection_results"
                    ).fetchone(),
                    ("source_error",),
                )

    def test_supervalu_source_failure_is_recorded_without_an_observation(self):
        mapping = SuperValuMapping(
            catalog_id=self.pack.catalog_id,
            expected_product_name="Coca-Cola Zero Sugar Can (330 ml)",
        )

        with tempfile.TemporaryDirectory() as directory:
            database = Path(directory) / "feed.sqlite"
            summary = collect_supervalu_one(
                self.pack,
                mapping,
                lambda _: (_ for _ in ()).throw(RuntimeError("SuperValu HTTP 503")),
                database,
                store_id="store-123",
            )

            self.assertEqual(summary["status"], "source_error")
            with closing(sqlite3.connect(database)) as connection:
                self.assertEqual(
                    connection.execute(
                        "SELECT COUNT(*) FROM price_observations"
                    ).fetchone()[0],
                    0,
                )
                self.assertEqual(
                    connection.execute(
                        "SELECT status, error FROM collection_results"
                    ).fetchone(),
                    ("source_error", "SuperValu HTTP 503"),
                )

    def test_current_feed_uses_latest_result_without_hiding_other_retailers(self):
        payload = {
            "data": {
                "productSearch": {
                    "products": [{
                        "productName": "Coca-Cola Zero Sugar 330ml",
                        "productReference": "COKE-ZERO-330",
                        "items": [{
                            "itemId": "COKE-ZERO-330-EA",
                            "sellers": [{"commertialOffer": {"Price": "2.49"}}],
                        }],
                    }]
                }
            }
        }
        supervalu_mapping = SuperValuMapping(
            catalog_id=self.pack.catalog_id,
            expected_product_name="Coca-Cola Zero Sugar Can (330 ml)",
            source_product_id="SV-330",
        )
        supervalu_payload = {
            "items": [{
                "productId": "SV-330",
                "sku": "SV-330-SKU",
                "name": "Coca-Cola Zero Sugar Can (330 ml)",
                "priceNumeric": "2.79",
            }]
        }

        with tempfile.TemporaryDirectory() as directory:
            database = Path(directory) / "feed.sqlite"
            collect_one(self.pack, self.mapping, lambda _: payload, database)
            collect_supervalu_one(
                self.pack, supervalu_mapping, lambda _: supervalu_payload,
                database, store_id="store-123",
            )
            # A later not-found result removes only Dunnes from the current feed.
            collect_one(self.pack, self.mapping, lambda _: {"data": {"productSearch": {"products": []}}}, database)

            feed = current_feed(database)
            history = price_history(database, retailer="dunnes", catalog_id=self.pack.catalog_id)
            seen = last_seen(database, retailer="dunnes", catalog_id=self.pack.catalog_id)

        self.assertEqual([row["retailer"] for row in feed], ["supervalu"])
        self.assertEqual(feed[0]["displayed_price"], "2.79")
        self.assertEqual(len(history), 1)
        self.assertEqual(seen["displayed_price"], "2.49")
        self.assertEqual(seen["observed_at"], history[0]["observed_at"])

    def test_price_history_keeps_observations_and_calculates_comparable_units(self):
        first_payload = {
            "data": {"productSearch": {"products": [{
                "productName": "Coca-Cola Zero Sugar 330ml",
                "productReference": "COKE-ZERO-330",
                "items": [{"itemId": "COKE-ZERO-330-EA", "sellers": [{
                    "commertialOffer": {"Price": "2.49"}
                }]}],
            }]}}
        }
        second_payload = json.loads(json.dumps(first_payload))
        second_payload["data"]["productSearch"]["products"][0]["items"][0]["sellers"][0]["commertialOffer"]["Price"] = "1.99"

        with tempfile.TemporaryDirectory() as directory:
            database = Path(directory) / "feed.sqlite"
            collect_one(self.pack, self.mapping, lambda _: first_payload, database)
            collect_one(self.pack, self.mapping, lambda _: second_payload, database)
            history = price_history(database)

        self.assertEqual([row["displayed_price"] for row in history], ["1.99", "2.49"])
        self.assertEqual(history[0]["component_unit_price"], "1.99")
        self.assertEqual(history[0]["price_per_litre"], "6.0303")
        self.assertNotIn("stock", history[0])

    def test_last_seen_is_none_when_pair_has_never_been_observed(self):
        with tempfile.TemporaryDirectory() as directory:
            database = Path(directory) / "feed.sqlite"
            result = last_seen(database, retailer="dunnes", catalog_id=self.pack.catalog_id)

        self.assertIsNone(result)

    def test_full_run_isolates_retailer_failures_and_persists_one_aggregate_run(self):
        dunnes_payload = {
            "data": {"productSearch": {"products": [{
                "productName": "Coca-Cola Zero Sugar 330ml",
                "productReference": "COKE-ZERO-330",
                "items": [{"itemId": "COKE-ZERO-330-EA", "sellers": [{
                    "commertialOffer": {"Price": "2.49"}
                }]}],
            }]}}
        }
        supervalu_mapping = SuperValuMapping(
            catalog_id=self.pack.catalog_id,
            expected_product_name="Coca-Cola Zero Sugar Can (330 ml)",
            source_product_id="SV-330",
        )
        tesco_mapping = TescoMapping(
            catalog_id=self.pack.catalog_id,
            expected_product_name="Coca-Cola Zero Sugar 330ml Can",
            source_tpnb="12345",
        )
        supervalu_payload = {"items": [{
            "productId": "SV-330", "sku": "SV-330-SKU",
            "name": "Coca-Cola Zero Sugar Can (330 ml)", "priceNumeric": "2.79",
        }]}
        tesco_payload = {"products": [{
            "tpnb": "12345", "id": "tesco-id",
            "title": "Coca-Cola Zero Sugar 330ml Can",
            "price": {"actual": "2.99"},
        }]}

        with tempfile.TemporaryDirectory() as directory:
            database = Path(directory) / "feed.sqlite"
            summary = collect_run(
                [self.pack],
                {
                    "dunnes": [self.mapping],
                    "supervalu": [supervalu_mapping],
                    "tesco": [tesco_mapping],
                },
                {
                    "dunnes": lambda _: dunnes_payload,
                    "supervalu": lambda _: (_ for _ in ()).throw(RuntimeError("503")),
                    "tesco": lambda _: tesco_payload,
                },
                database,
                store_ids={"supervalu": "store-123"},
            )
            with closing(sqlite3.connect(database)) as connection:
                result_count = connection.execute("SELECT COUNT(*) FROM collection_results").fetchone()[0]
                observation_count = connection.execute("SELECT COUNT(*) FROM price_observations").fetchone()[0]
                run_count = connection.execute("SELECT COUNT(*) FROM collection_runs").fetchone()[0]

            feed = current_feed(database)

        self.assertEqual(summary["status"], "partial")
        self.assertEqual(summary["attempted_count"], 3)
        self.assertEqual(summary["observed_count"], 2)
        self.assertEqual(summary["failed_count"], 1)
        self.assertEqual((result_count, observation_count, run_count), (3, 2, 1))
        self.assertEqual({row["retailer"] for row in feed}, {"dunnes", "tesco"})
        self.assertEqual(summary["affected_retailers"], ["supervalu"])

    def test_main_runs_configured_retailer_matrix(self):
        catalog = [{
            "catalog_id": self.pack.catalog_id,
            "name": self.pack.name,
            "brand": self.pack.brand,
            "variant": self.pack.variant,
            "pack_count": self.pack.pack_count,
            "unit_size_ml": self.pack.unit_size_ml,
            "package_type": self.pack.package_type,
            "search_term": self.pack.search_term,
            "aliases": [],
        }]
        mappings = {
            "dunnes": [{
                "catalog_id": self.pack.catalog_id,
                "expected_product_name": self.mapping.expected_product_name,
                "source_product_reference": self.mapping.source_product_reference,
                "source_item_id": self.mapping.source_item_id,
                "status": "approved",
            }],
            "supervalu": [{
                "catalog_id": self.pack.catalog_id,
                "expected_product_name": "Coca-Cola Zero Sugar Can (330 ml)",
                "source_product_id": "SV-330",
                "status": "approved",
            }],
            "tesco": [{
                "catalog_id": self.pack.catalog_id,
                "expected_product_name": "Coca-Cola Zero Sugar 330ml Can",
                "source_tpnb": "12345",
                "status": "approved",
            }],
        }
        dunnes_payload = {
            "data": {"productSearch": {"products": [{
                "productName": self.pack.name,
                "productReference": "COKE-ZERO-330",
                "items": [{"itemId": "COKE-ZERO-330-EA", "sellers": [{
                    "commertialOffer": {"Price": "2.49"}
                }]}],
            }]}}
        }
        supervalu_payload = {"items": [{
            "productId": "SV-330", "sku": "SV-330-SKU",
            "name": "Coca-Cola Zero Sugar Can (330 ml)", "priceNumeric": "2.79",
        }]}
        tesco_payload = {"products": [{
            "tpnb": "12345", "id": "tesco-id",
            "title": "Coca-Cola Zero Sugar 330ml Can",
            "price": {"actual": "2.99"},
        }]}

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            catalog_path = root / "catalog.json"
            mapping_path = root / "mappings.json"
            database = root / "feed.sqlite"
            catalog_path.write_text(json.dumps(catalog))
            mapping_path.write_text(json.dumps(mappings))
            with patch("beverage_feed.collector.DunnesClient", return_value=lambda _: dunnes_payload), \
                 patch("beverage_feed.collector.SuperValuClient", return_value=lambda _: supervalu_payload), \
                 patch("beverage_feed.collector.TescoClient", return_value=lambda _: tesco_payload):
                with redirect_stdout(io.StringIO()) as output:
                    result = main([
                        "--catalog", str(catalog_path),
                        "--mapping", str(mapping_path),
                        "--database", str(database),
                        "--supervalu-store-id", "store-123",
                    ])
            with closing(sqlite3.connect(database)) as connection:
                observed = connection.execute(
                    "SELECT COUNT(*) FROM price_observations"
                ).fetchone()[0]

        self.assertEqual(result, 0)
        self.assertEqual(observed, 3)
        self.assertIn("attempted=3", output.getvalue())
        self.assertIn("mapped=3", output.getvalue())
        self.assertIn("unmapped=0", output.getvalue())

    def test_run_summary_separates_unmapped_coverage_from_failures(self):
        empty_payload = {"data": {"productSearch": {"products": []}}}
        with tempfile.TemporaryDirectory() as directory:
            database = Path(directory) / "feed.sqlite"
            summary = collect_run(
                [self.pack],
                {"dunnes": [self.mapping], "tesco": []},
                {
                    "dunnes": lambda _: empty_payload,
                    "tesco": lambda _: (_ for _ in ()).throw(
                        AssertionError("unmapped work must not call the adapter")
                    ),
                },
                database,
            )
            with closing(sqlite3.connect(database)) as connection:
                diagnostics = connection.execute(
                    "SELECT event, level FROM collection_diagnostics"
                ).fetchall()

        self.assertEqual(summary["attempted_count"], 2)
        self.assertEqual(summary["mapped_count"], 1)
        self.assertEqual(summary["not_found_count"], 1)
        self.assertEqual(summary["unmapped_count"], 1)
        self.assertEqual(summary["affected_retailers"], ["dunnes"])
        self.assertEqual(summary["unmapped_retailers"], ["tesco"])
        self.assertEqual(summary["affected_catalog_ids"], [self.pack.catalog_id])
        self.assertEqual(summary["unmapped_catalog_ids"], [self.pack.catalog_id])
        self.assertIn(("unmapped_summary", "info"), diagnostics)
        self.assertEqual(diagnostics.count(("result", "warning")), 1)

    def test_full_run_uses_known_tesco_tpnb_without_search(self):
        mapping = TescoMapping(
            catalog_id=self.pack.catalog_id,
            expected_product_name="Coca-Cola Zero Sugar 330ml Can",
            source_tpnb="12345",
        )
        calls = []
        payload = {"products": [{
            "tpnb": "12345",
            "id": "tesco-id",
            "title": "Coca-Cola Zero Sugar 330ml Can",
            "price": {"actual": "2.49"},
        }]}

        class TescoAdapter:
            def __call__(self, _search_term):
                raise AssertionError("search fetch should not be used")

            def fetch_product(self, tpnb):
                calls.append(tpnb)
                return payload

        with tempfile.TemporaryDirectory() as directory:
            summary = collect_run(
                [self.pack], {"tesco": [mapping]}, {"tesco": TescoAdapter()},
                Path(directory) / "feed.sqlite", max_retries=0,
            )

        self.assertEqual(summary["observed_count"], 1)
        self.assertEqual(calls, ["12345"])

    def test_targeted_run_only_attempts_requested_retailer_and_pack(self):
        calls = []
        payload = {"data": {"productSearch": {"products": []}}}
        with tempfile.TemporaryDirectory() as directory:
            summary = collect_run(
                [self.pack],
                {"dunnes": [self.mapping], "tesco": []},
                {
                    "dunnes": lambda term: calls.append(("dunnes", term)) or payload,
                    "tesco": lambda term: calls.append(("tesco", term)) or payload,
                },
                Path(directory) / "feed.sqlite",
                retailer="dunnes",
                catalog_id=self.pack.catalog_id,
            )

        self.assertEqual(calls, [("dunnes", self.pack.search_term)])
        self.assertEqual(summary["attempted_count"], 1)
        self.assertEqual(summary["not_found_count"], 1)

    def test_retries_are_capped_and_diagnostics_keep_raw_response_without_headers(self):
        attempts = []
        payload = {"products": [{"tpnb": "12345", "title": "Coke", "price": {"actual": "2.49"}}]}
        mapping = TescoMapping(
            catalog_id=self.pack.catalog_id,
            expected_product_name="Coke",
            source_tpnb="12345",
        )

        def fetch(_):
            attempts.append(True)
            if len(attempts) == 1:
                raise RuntimeError("temporary outage")
            return payload

        with tempfile.TemporaryDirectory() as directory:
            database = Path(directory) / "feed.sqlite"
            collect_run(
                [self.pack], {"tesco": [mapping]}, {"tesco": fetch}, database,
                max_retries=1, retry_backoff=0,
            )
            with closing(sqlite3.connect(database)) as connection:
                events = connection.execute(
                    "SELECT event, raw_record, request_metadata FROM collection_diagnostics ORDER BY diagnostic_id"
                ).fetchall()

        self.assertEqual(len(attempts), 2)
        fetch_events = [event for event in events if event[0] != "collection_fallback"]
        self.assertEqual([event[0] for event in fetch_events], ["request", "error", "retry", "request", "response"])
        response_event = fetch_events[-1]
        self.assertIn('"tpnb": "12345"', response_event[1])
        self.assertNotIn("apikey", (response_event[1] or "").lower())
        self.assertNotIn("authorization", (response_event[2] or "").lower())
        # The mapped TPNB had no direct-hydration path, so the search fallback
        # is surfaced to the operator as a distinct diagnostic.
        self.assertIn("collection_fallback", [event[0] for event in events])

    def test_retention_marks_stale_mappings_dormant_and_purges_old_detail(self):
        payload = {
            "data": {"productSearch": {"products": [{
                "productName": "Coca-Cola Zero Sugar 330ml",
                "productReference": "COKE-ZERO-330",
                "items": [{"itemId": "COKE-ZERO-330-EA", "sellers": [{
                    "commertialOffer": {"Price": "2.49"}
                }]}],
            }]}}
        }
        with tempfile.TemporaryDirectory() as directory:
            database = Path(directory) / "feed.sqlite"
            collect_run([self.pack], {"dunnes": [self.mapping]}, {"dunnes": lambda _: payload}, database)
            old = "2020-07-01T00:00:00Z"
            with closing(sqlite3.connect(database)) as connection:
                connection.execute("UPDATE price_observations SET observed_at = ?", (old,))
                connection.execute("UPDATE collection_diagnostics SET created_at = ?", (old,))
                connection.commit()
            dormant_counts = purge_retention(database, now="2021-01-01T00:00:00Z")
            with closing(sqlite3.connect(database)) as connection:
                mapping_status = connection.execute("SELECT status FROM catalog_mappings").fetchone()
                observations_before_purge = connection.execute("SELECT COUNT(*) FROM price_observations").fetchone()[0]
            purge_counts = purge_retention(database, now="2022-01-01T00:00:00Z")
            with closing(sqlite3.connect(database)) as connection:
                observations = connection.execute("SELECT COUNT(*) FROM price_observations").fetchone()[0]
                runs = connection.execute("SELECT COUNT(*) FROM collection_runs").fetchone()[0]

        self.assertEqual(mapping_status, ("dormant",))
        self.assertEqual(observations_before_purge, 1)
        self.assertEqual(observations, 0)
        self.assertEqual(runs, 1)
        self.assertEqual(dormant_counts["dormant_mappings"], 1)
        self.assertGreaterEqual(purge_counts["purged_observations"], 1)

    def test_source_failure_is_recorded_without_an_observation(self):
        def fail(_):
            raise RuntimeError("Dunnes returned HTTP 503")

        with tempfile.TemporaryDirectory() as directory:
            database = Path(directory) / "feed.sqlite"
            summary = collect_one(self.pack, self.mapping, fail, database)

            self.assertEqual(summary["status"], "source_error")
            self.assertEqual(summary["observed_count"], 0)
            with closing(sqlite3.connect(database)) as connection:
                result = connection.execute(
                    "SELECT status, error FROM collection_results"
                ).fetchone()
                observations = connection.execute(
                    "SELECT COUNT(*) FROM price_observations"
                ).fetchone()[0]

            self.assertEqual(result, ("source_error", "Dunnes returned HTTP 503"))
            self.assertEqual(observations, 0)


if __name__ == "__main__":
    unittest.main()
