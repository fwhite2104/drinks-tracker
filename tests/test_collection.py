import io
import json
import sqlite3
import tempfile
from contextlib import closing, redirect_stdout
from decimal import Decimal
import unittest
from pathlib import Path
from unittest.mock import patch

from beverage_feed.collector import (
    AldiClient,
    AldiMapping,
    BenchmarkPack,
    DunnesMapping,
    LidlClient,
    LidlMapping,
    SuperValuClient,
    SuperValuMapping,
    TescoClient,
    TescoMapping,
    _aldi_drs_deposit,
    _dunnes_drs_deposit,
    _lidl_drs_deposit,
    _validate_listing,
    collect_aldi_one,
    collect_catalog,
    collect_lidl_one,
    collect_one,
    collect_supervalu_one,
    collect_tesco_one,
    collect_run,
    current_feed,
    ensure_schema,
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

    def test_dunnes_malformed_price_is_a_source_error(self):
        payload = {
            "data": {
                "productSearch": {
                    "products": [{
                        "productName": "Coca-Cola Zero Sugar 330ml",
                        "productReference": "COKE-ZERO-330",
                        "items": [{
                            "itemId": "COKE-ZERO-330-EA",
                            "sellers": [{"commertialOffer": {"Price": "not-a-price"}}],
                        }],
                    }]
                }
            }
        }
        with tempfile.TemporaryDirectory() as directory:
            database = Path(directory) / "feed.sqlite"
            summary = collect_one(self.pack, self.mapping, lambda _: payload, database)
            with closing(sqlite3.connect(database)) as connection:
                observations = connection.execute(
                    "SELECT COUNT(*) FROM price_observations"
                ).fetchone()[0]
                result = connection.execute(
                    "SELECT status FROM collection_results"
                ).fetchone()
        self.assertEqual(summary["status"], "source_error")
        self.assertEqual(summary["observed_count"], 0)
        self.assertEqual(summary["failed_count"], 1)
        self.assertEqual(observations, 0)
        self.assertEqual(result, ("source_error",))

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

    def test_current_feed_keeps_one_row_when_run_has_multiple_observations(self):
        with tempfile.TemporaryDirectory() as directory:
            database = Path(directory) / "feed.sqlite"
            with closing(sqlite3.connect(database)) as connection:
                ensure_schema(connection)
                connection.execute(
                    "INSERT INTO catalog_packs VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                    (
                        self.pack.catalog_id, self.pack.name, self.pack.brand,
                        self.pack.variant, self.pack.pack_count,
                        self.pack.unit_size_ml, self.pack.package_type,
                        self.pack.search_term,
                    ),
                )
                connection.execute(
                    "INSERT INTO collection_runs VALUES (?, ?, ?, ?, ?, ?, ?)",
                    ("run-1", "t", "t", "completed", 2, 0, "{}"),
                )
                connection.execute(
                    "INSERT INTO collection_results VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (
                        "run-1", self.pack.catalog_id, "tesco", "observed",
                        None, "tpnb", "tpnb", None, "2026-01-01T00:00:00Z",
                    ),
                )
                for price, observed_at in (("1.00", "2026-01-01T00:00:00Z"),
                                           ("1.10", "2026-01-01T00:00:01Z")):
                    connection.execute(
                        """
                        INSERT INTO price_observations (
                            run_id, catalog_id, retailer, source_product_reference,
                            source_item_id, source_product_name, displayed_price,
                            currency, pack_count, unit_size_ml, package_type, observed_at
                        ) VALUES (?, ?, 'tesco', 'tpnb', 'tpnb', ?, ?, 'EUR', 1, 330, 'can', ?)
                        """,
                        ("run-1", self.pack.catalog_id, self.pack.name, price, observed_at),
                    )
                connection.commit()
            feed = current_feed(database)

        self.assertEqual(len(feed), 1)
        self.assertEqual(feed[0]["displayed_price"], "1.10")

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
        payload = {"products": [{"tpnb": "12345", "title": "Coca-Cola Zero Sugar 330ml Can", "price": {"actual": "2.49"}}]}
        mapping = TescoMapping(
            catalog_id=self.pack.catalog_id,
            expected_product_name="Coca-Cola Zero Sugar 330ml Can",
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

    def test_stale_tesco_tpnb_records_source_error_not_false_price(self):
        """When a mapped TPNB returns a different product, it is a source_error."""
        payload = {"products": [{"tpnb": "12345", "title": "Pepsi Max 330ml Can", "price": {"actual": "2.49"}}]}
        mapping = TescoMapping(
            catalog_id=self.pack.catalog_id,
            expected_product_name="Coca-Cola Zero Sugar 330ml Can",
            source_tpnb="12345",
        )

        with tempfile.TemporaryDirectory() as directory:
            database = Path(directory) / "feed.sqlite"
            summary = collect_tesco_one(self.pack, mapping, lambda _: payload, database)

            self.assertEqual(summary["status"], "source_error")
            self.assertIn("name mismatch", summary["error"])

    def test_stale_supervalu_product_id_records_source_error(self):
        """When a mapped SuperValu product ID returns a different product."""
        payload = {"items": [{
            "productId": "SV-330", "sku": "SV-330-SKU",
            "name": "Pepsi Max Can (330 ml)", "priceNumeric": "2.49",
        }]}
        mapping = SuperValuMapping(
            catalog_id=self.pack.catalog_id,
            expected_product_name="Coca-Cola Zero Sugar Can (330 ml)",
            source_product_id="SV-330",
        )

        with tempfile.TemporaryDirectory() as directory:
            database = Path(directory) / "feed.sqlite"
            summary = collect_supervalu_one(
                self.pack, mapping, lambda _: payload, database, store_id="s1",
            )

            self.assertEqual(summary["status"], "source_error")
            self.assertIn("name mismatch", summary["error"])

    def test_supervalu_direct_hydration_via_fetch_product(self):
        """SuperValu collect_one uses fetch_product when source_product_id is known."""
        calls = []

        class HydratingClient:
            store_id = "s1"

            def __call__(self, term):
                calls.append(("search", term))
                return {"items": []}

            def fetch_product(self, product_id):
                calls.append(("hydrate", product_id))
                return {
                    "productId": "SV-330", "sku": "SV-330-SKU",
                    "name": "Coca-Cola Zero Sugar Can (330 ml)", "priceNumeric": "2.49",
                }

        mapping = SuperValuMapping(
            catalog_id=self.pack.catalog_id,
            expected_product_name="Coca-Cola Zero Sugar Can (330 ml)",
            source_product_id="SV-330",
        )

        with tempfile.TemporaryDirectory() as directory:
            database = Path(directory) / "feed.sqlite"
            summary = collect_supervalu_one(
                self.pack, mapping, HydratingClient(), database,
            )

            self.assertEqual(summary["status"], "observed")
            self.assertEqual(calls, [("hydrate", "SV-330")])  # direct hydration, no search

    # --- Lidl Ireland ---

    def _lidl_search_payload(self, *, price=1.99, old_price=0.0, base_price_text="1l = 9.95",
                             packaging="200ml", name="HATA Cola Drink", special_taxes=None):
        """Captured /q/api/search shape (trimmed to the retained evidence)."""
        price_block = {
            "currencyCode": "EUR",
            "price": price,
            "oldPrice": old_price,
            "basePrice": {"prefix": False, "text": base_price_text},
            "specialTaxes": special_taxes if special_taxes is not None else [],
        }
        if packaging is not None:
            price_block["packaging"] = {"text": packaging}
        return {
            "type": "search",
            "resultType": "search",
            "q": name,
            "numFound": 1,
            "offset": 0,
            "fetchsize": 100,
            "items": [{
                "resultClass": "product",
                "code": "10062229",
                "gridbox": {"data": {
                    "fullTitle": name,
                    "title": name,
                    "productId": 10062229,
                    "itemId": 10062229,
                    "erpNumber": "10062229",
                    "canonicalPath": "/p/hata-cola-drink/p10062229",
                    "multipack": False,
                    "price": price_block,
                }},
            }],
        }

    def _lidl_product_page(self, *, product_id="10062229", name="HATA Cola Drink",
                           price=1.99, base_price_text="1l = 9.95", packaging="200ml"):
        """Captured pdp-view __NUXT_DATA__ shape (trimmed to retained evidence)."""
        elements = [
            ["ShallowReactive", 1],
            {"data": 2, "serverRendered": 5},
            ["ShallowReactive", 3],
            {"product": 4},
            {"erpNumber": 6, "productId": 7, "canonicalPath": 8, "keyfacts": 9, "multipack": 10},
            True,
            str(product_id),
            int(product_id),
            f"/p/hata-cola-drink/p{product_id}",
            {"fullTitle": 11, "title": 11},
            False,
            name,
            {"basePrice": 13, "oldPrice": 14, "price": 15, "specialTaxes": 16},
            {"prefix": 10, "text": 17},
            0,
            price,
            [],
            base_price_text,
        ]
        if packaging is not None:
            elements[12]["packaging"] = 18
            elements.append({"text": 19})
            elements.append(packaging)
        script = json.dumps(elements).replace("</", "<\\/")
        return (
            "<!doctype html><html><head>"
            '<script type="application/json" data-nuxt-data="pdp-view" data-ssr="true" id="__NUXT_DATA__">'
            f"{script}</script></head><body></body></html>"
        )

    def test_lidl_client_searches_and_normalizes_gridbox_records(self):
        requests = []

        class Enterable(io.BytesIO):
            status = 200

            def __enter__(self):
                return self

            def __exit__(self, *args):
                self.close()

        responses = [Enterable(json.dumps(self._lidl_search_payload(old_price=2.99)).encode())]

        class Opener:
            def open(self, request, timeout):
                requests.append(request)
                return responses.pop(0)

        client = LidlClient(opener=Opener(), min_request_interval=0)
        payload = client("HATA Cola Drink")

        record = payload["items"][0]
        self.assertEqual(record["productId"], "10062229")
        self.assertEqual(record["name"], "HATA Cola Drink")
        self.assertEqual(record["price"], 1.99)
        self.assertEqual(record["oldPrice"], 2.99)
        self.assertEqual(record["basePriceText"], "1l = 9.95")
        self.assertEqual(record["packSize"], "200ml")
        self.assertEqual(record["specialTaxes"], [])
        self.assertEqual(payload["pagination"], {"total": 1, "offset": 0})
        url = requests[0].full_url
        self.assertIn("https://www.lidl.ie/q/api/search?", url)
        self.assertIn("assortment=IE", url)
        self.assertIn("q=HATA+Cola+Drink", url)
        self.assertEqual(requests[0].get_header("Accept"), "application/mindshift.search+json")
        self.assertEqual(requests[0].get_header("User-agent"), "drinks-tracker/0.1")

    def test_lidl_client_treats_empty_result_type_as_no_items(self):
        class Enterable(io.BytesIO):
            status = 200

            def __enter__(self):
                return self

            def __exit__(self, *args):
                self.close()

        class Opener:
            def open(self, request, timeout):
                return Enterable(json.dumps({
                    "type": "empty", "resultType": "empty", "q": "ballygowan",
                }).encode())

        client = LidlClient(opener=Opener(), min_request_interval=0)
        payload = client("ballygowan")

        self.assertEqual(payload["items"], [])
        self.assertEqual(payload["pagination"], {"total": 0, "offset": 0})

    def test_lidl_client_network_failure_raises_useful_error(self):
        class Opener:
            def open(self, request, timeout):
                raise OSError("connection reset")

        client = LidlClient(opener=Opener(), min_request_interval=0)
        with self.assertRaises(RuntimeError) as context:
            client("HATA Cola Drink")
        self.assertIn("Lidl request failed", str(context.exception))
        self.assertIn("connection reset", str(context.exception))

    def test_lidl_client_hydrates_product_page_through_redirect(self):
        requests = []

        class Enterable(io.BytesIO):
            status = 200

            def __enter__(self):
                return self

            def __exit__(self, *args):
                self.close()

        responses = [
            Enterable(json.dumps({
                "type": "redirect", "resultType": "redirect", "q": "10062229",
                "redirectURL": "/p/hata-cola-drink/p10062229",
            }).encode()),
            Enterable(self._lidl_product_page().encode()),
        ]

        class Opener:
            def open(self, request, timeout):
                requests.append(request)
                return responses.pop(0)

        client = LidlClient(opener=Opener(), min_request_interval=0)
        payload = client.fetch_product("10062229")

        record = payload["items"][0]
        self.assertEqual(record["productId"], "10062229")
        self.assertEqual(record["name"], "HATA Cola Drink")
        self.assertEqual(record["price"], 1.99)
        self.assertEqual(record["basePriceText"], "1l = 9.95")
        self.assertEqual(record["packSize"], "200ml")
        self.assertEqual(requests[1].full_url, "https://www.lidl.ie/p/hata-cola-drink/p10062229")

    def test_lidl_client_fetch_product_without_redirect_returns_no_items(self):
        class Enterable(io.BytesIO):
            status = 200

            def __enter__(self):
                return self

            def __exit__(self, *args):
                self.close()

        responses = [Enterable(json.dumps({
            "type": "empty", "resultType": "empty", "q": "99999999",
        }).encode())]

        class Opener:
            def open(self, request, timeout):
                return responses.pop(0)

        client = LidlClient(opener=Opener(), min_request_interval=0)
        self.assertEqual(client.fetch_product("99999999"), {"items": []})

    def test_collects_lidl_price_without_loyalty_and_with_deposit(self):
        record = {
            "productId": "10062229",
            "name": "Coca-Cola Zero Sugar 330ml Can",
            "price": 2.49,
            "basePriceText": "\u20ac0.15 Deposit Return",
            "specialTaxes": [],
        }
        mapping = LidlMapping(
            catalog_id=self.pack.catalog_id,
            expected_product_name="Coca-Cola Zero Sugar 330ml Can",
            source_product_id="10062229",
        )

        class HydratingClient:
            def fetch_product(self, product_id):
                return {"items": [record]}

            def __call__(self, term):
                raise AssertionError("direct hydration should be used")

        with tempfile.TemporaryDirectory() as directory:
            database = Path(directory) / "feed.sqlite"
            summary = collect_lidl_one(self.pack, mapping, HydratingClient(), database)

            self.assertEqual(summary["status"], "observed")
            self.assertEqual(summary["retailer"], "lidl")
            with closing(sqlite3.connect(database)) as connection:
                observation = connection.execute(
                    """
                    SELECT retailer, source_product_reference, source_item_id,
                           displayed_price, clubcard_price, drs_deposit, source_scope,
                           currency, component_unit_price, price_per_litre
                    FROM price_observations
                    """
                ).fetchone()
                result = connection.execute(
                    "SELECT status, retailer FROM collection_results"
                ).fetchone()

        self.assertEqual(
            observation,
            ("lidl", "10062229", "10062229", "2.49", None, "0.15", None,
             "EUR", "2.49", "7.5455"),
        )
        self.assertEqual(result, ("observed", "lidl"))

    def test_lidl_no_match_is_not_found(self):
        mapping = LidlMapping(
            catalog_id=self.pack.catalog_id,
            expected_product_name="Coca-Cola Zero Sugar 330ml Can",
        )

        with tempfile.TemporaryDirectory() as directory:
            database = Path(directory) / "feed.sqlite"
            summary = collect_lidl_one(
                self.pack, mapping,
                lambda _: {"items": [{"productId": "10062229", "name": "HATA Cola Drink"}]},
                database,
            )

            self.assertEqual(summary["status"], "not_found")
            self.assertEqual(summary["failed_count"], 0)
            with closing(sqlite3.connect(database)) as connection:
                observations = connection.execute(
                    "SELECT COUNT(*) FROM price_observations"
                ).fetchone()[0]
                result = connection.execute(
                    "SELECT status FROM collection_results"
                ).fetchone()
            self.assertEqual(observations, 0)
            self.assertEqual(result, ("not_found",))

    def test_lidl_missing_price_is_a_source_error(self):
        record = {
            "productId": "10062229",
            "name": "Coca-Cola Zero Sugar 330ml Can",
            "specialTaxes": [],
        }
        mapping = LidlMapping(
            catalog_id=self.pack.catalog_id,
            expected_product_name="Coca-Cola Zero Sugar 330ml Can",
            source_product_id="10062229",
        )

        with tempfile.TemporaryDirectory() as directory:
            database = Path(directory) / "feed.sqlite"
            summary = collect_lidl_one(
                self.pack, mapping, lambda _: {"items": [record]}, database,
            )

            self.assertEqual(summary["status"], "source_error")
            self.assertIn("no price", summary["error"])
            with closing(sqlite3.connect(database)) as connection:
                observations = connection.execute(
                    "SELECT COUNT(*) FROM price_observations"
                ).fetchone()[0]
            self.assertEqual(observations, 0)

    def test_lidl_drs_deposit_is_parsed_from_isolated_evidence(self):
        self.assertEqual(
            _lidl_drs_deposit({"basePriceText": "\u20ac2.25 Deposit Return"}),
            Decimal("2.25"),
        )
        self.assertEqual(
            _lidl_drs_deposit({"specialTaxes": [{"label": "Deposit Return", "amount": "0.15"}]}),
            Decimal("0.15"),
        )
        # Unit-price text is not deposit evidence.
        self.assertIsNone(_lidl_drs_deposit({"basePriceText": "1l = 9.95"}))
        self.assertIsNone(_lidl_drs_deposit({}))
        self.assertIsNone(
            _lidl_drs_deposit({"specialTaxes": [{"label": "VAT", "amount": "0.23"}]})
        )

    def test_lidl_source_failure_does_not_replace_previous_observation(self):
        good = {
            "productId": "10062229",
            "name": "Coca-Cola Zero Sugar 330ml Can",
            "price": 2.49,
            "specialTaxes": [],
        }
        mapping = LidlMapping(
            catalog_id=self.pack.catalog_id,
            expected_product_name="Coca-Cola Zero Sugar 330ml Can",
            source_product_id="10062229",
        )

        def failing_fetch(_):
            raise RuntimeError("Lidl request failed: outage")

        with tempfile.TemporaryDirectory() as directory:
            database = Path(directory) / "feed.sqlite"
            collect_lidl_one(self.pack, mapping, lambda _: {"items": [good]}, database)
            summary = collect_lidl_one(self.pack, mapping, failing_fetch, database)

            self.assertEqual(summary["status"], "source_error")
            history = price_history(database, retailer="lidl")
            self.assertEqual(len(history), 1)
            self.assertEqual(history[0]["displayed_price"], "2.49")
    # --- Aldi Ireland ---

    def _aldi_search_payload(self, *, name="Coca-Cola Zero Sugar 330ml Can",
                             amount=249, display="\u20ac2.49", was=None,
                             deposit=0, deposit_display="\u20ac0.00"):
        """Captured asl.api.aldi.ie product-search shape (trimmed)."""
        return {
            "meta": {"pagination": {"offset": 0, "limit": 30, "totalCount": 1}},
            "data": [{
                "sku": "000000000728654001",
                "name": name,
                "brandName": "COCA-COLA",
                "urlSlugText": "coca-cola-zero-sugar-330ml-can",
                "sellingSize": "330 ML",
                "price": {
                    "amount": amount,
                    "amountRelevant": amount,
                    "amountRelevantDisplay": display,
                    "bottleDeposit": deposit,
                    "bottleDepositDisplay": deposit_display,
                    "comparison": 755,
                    "comparisonDisplay": "\u20ac7.55/1 L",
                    "currencyCode": "EUR",
                    "currencySymbol": "\u20ac",
                    "wasPriceDisplay": was,
                },
            }],
        }

    def _aldi_opener(self, payloads):
        responses = list(payloads)
        requests = []

        class Enterable(io.BytesIO):
            status = 200

            def __enter__(self):
                return self

            def __exit__(self, *args):
                self.close()

        class Opener:
            def open(self, request, timeout):
                requests.append(request)
                body = responses.pop(0)
                return Enterable(body.encode() if isinstance(body, str) else body)

        return Opener(), requests

    def test_aldi_client_searches_and_normalizes_records(self):
        opener, requests = self._aldi_opener(
            [json.dumps(self._aldi_search_payload(was="\u20ac2.99"))]
        )
        client = AldiClient(opener=opener, min_request_interval=0)
        payload = client("Coca-Cola Zero Sugar")

        record = payload["items"][0]
        self.assertEqual(record["productId"], "000000000728654001")
        self.assertEqual(record["name"], "Coca-Cola Zero Sugar 330ml Can")
        self.assertEqual(record["brand"], "COCA-COLA")
        self.assertEqual(record["price"], "\u20ac2.49")
        self.assertEqual(record["oldPrice"], "\u20ac2.99")
        self.assertEqual(record["unitPriceText"], "\u20ac7.55/1 L")
        self.assertEqual(record["totalVolume"], "330 ML")
        self.assertNotIn("bottleDepositText", record)  # zero deposit means no deposit
        self.assertEqual(payload["pagination"], {"total": 1, "offset": 0})
        url = requests[0].full_url
        self.assertIn("https://asl.api.aldi.ie/commerce/v3/product-search?", url)
        self.assertIn("q=Coca-Cola+Zero+Sugar", url)
        self.assertIn("limit=30", url)
        self.assertEqual(requests[0].get_header("User-agent"), "drinks-tracker/0.1")

    def test_aldi_client_derives_euro_price_from_cents_without_display_string(self):
        payload = self._aldi_search_payload()
        del payload["data"][0]["price"]["amountRelevantDisplay"]
        opener, _ = self._aldi_opener([json.dumps(payload)])
        client = AldiClient(opener=opener, min_request_interval=0)

        record = client("cola")["items"][0]

        self.assertIsInstance(record["price"], str)
        self.assertEqual(Decimal(record["price"].replace("\u20ac", "")), Decimal("2.49"))

    def test_aldi_client_network_failure_raises_useful_error(self):
        class Opener:
            def open(self, request, timeout):
                raise OSError("connection refused")

        client = AldiClient(opener=Opener(), min_request_interval=0)
        with self.assertRaises(RuntimeError) as context:
            client("cola")
        self.assertIn("Aldi request failed", str(context.exception))

    def test_aldi_client_hydrates_known_sku_and_ignores_mismatches(self):
        opener, requests = self._aldi_opener([
            json.dumps({"meta": {}, "data": [
                self._aldi_search_payload()["data"][0],
            ]}),
            json.dumps({"meta": {}, "data": []}),
        ])
        client = AldiClient(opener=opener, min_request_interval=0)

        payload = client.fetch_product("000000000728654001")
        missing = client.fetch_product("999999999999999")

        self.assertEqual(payload["items"][0]["productId"], "000000000728654001")
        self.assertEqual(missing, {"items": []})
        self.assertIn("/commerce/v2/products?", requests[1].full_url)
        self.assertIn("skus=999999999999999", requests[1].full_url)

    def test_collects_aldi_price_without_loyalty_and_with_deposit(self):
        record = {
            "productId": "000000000728654001",
            "name": "Coca-Cola Zero Sugar 330ml Can",
            "brand": "COCA-COLA",
            "price": "\u20ac2.49",
            "bottleDepositText": "\u20ac0.15",
        }
        mapping = AldiMapping(
            catalog_id=self.pack.catalog_id,
            expected_product_name="Coca-Cola Zero Sugar 330ml Can",
            source_product_id="000000000728654001",
        )

        class HydratingClient:
            def fetch_product(self, product_id):
                return {"items": [record]}

            def __call__(self, term):
                raise AssertionError("direct hydration should be used")

        with tempfile.TemporaryDirectory() as directory:
            database = Path(directory) / "feed.sqlite"
            summary = collect_aldi_one(self.pack, mapping, HydratingClient(), database)

            self.assertEqual(summary["status"], "observed")
            self.assertEqual(summary["retailer"], "aldi")
            with closing(sqlite3.connect(database)) as connection:
                observation = connection.execute(
                    """
                    SELECT retailer, source_product_reference, displayed_price,
                           clubcard_price, drs_deposit, currency,
                           component_unit_price, price_per_litre
                    FROM price_observations
                    """
                ).fetchone()
                result = connection.execute(
                    "SELECT status FROM collection_results"
                ).fetchone()

        self.assertEqual(
            observation,
            ("aldi", "000000000728654001", "2.49", None, "0.15",
             "EUR", "2.49", "7.5455"),
        )
        self.assertEqual(result, ("observed",))

    def test_aldi_no_match_is_not_found(self):
        mapping = AldiMapping(
            catalog_id=self.pack.catalog_id,
            expected_product_name="Coca-Cola Zero Sugar 330ml Can",
        )

        with tempfile.TemporaryDirectory() as directory:
            database = Path(directory) / "feed.sqlite"
            summary = collect_aldi_one(
                self.pack, mapping,
                lambda _: {"items": [{"productId": "1", "name": "Still Water"}]},
                database,
            )

            self.assertEqual(summary["status"], "not_found")
            with closing(sqlite3.connect(database)) as connection:
                observations = connection.execute(
                    "SELECT COUNT(*) FROM price_observations"
                ).fetchone()[0]
            self.assertEqual(observations, 0)

    def test_aldi_missing_price_is_a_source_error(self):
        mapping = AldiMapping(
            catalog_id=self.pack.catalog_id,
            expected_product_name="Coca-Cola Zero Sugar 330ml Can",
            source_product_id="000000000728654001",
        )

        with tempfile.TemporaryDirectory() as directory:
            database = Path(directory) / "feed.sqlite"
            summary = collect_aldi_one(
                self.pack, mapping,
                lambda _: {"items": [{
                    "productId": "000000000728654001",
                    "name": "Coca-Cola Zero Sugar 330ml Can",
                }]},
                database,
            )

            self.assertEqual(summary["status"], "source_error")
            self.assertIn("no price", summary["error"])

    def test_aldi_drs_deposit_is_parsed_from_isolated_evidence(self):
        self.assertEqual(_aldi_drs_deposit({"bottleDepositText": "\u20ac0.15"}), Decimal("0.15"))
        # A zero-deposit display string is never stored as evidence, but if it
        # ever reaches the helper it must parse as zero, not fail.
        self.assertEqual(_aldi_drs_deposit({"bottleDepositText": "\u20ac0.00"}), Decimal("0.00"))
        self.assertIsNone(_aldi_drs_deposit({}))

    def test_aldi_structured_brand_evidence_passes_staleness_check(self):
        """Brand living in the structured field, not the name, is not drift."""
        record = {
            "productId": "000000000000336021",
            "name": "Still Water",
            "brand": "COMERAGH",
            "price": "\u20ac1.45",
        }
        pack = BenchmarkPack(
            catalog_id="comeragh-still-water-5l",
            name="Comeragh Still Water 5L Bottle",
            brand="Comeragh", variant="Still Water", pack_count=1,
            unit_size_ml=5000, package_type="bottle", search_term="Still Water",
        )
        mapping = AldiMapping(
            catalog_id=pack.catalog_id,
            expected_product_name="Still Water",
            source_product_id="000000000000336021",
        )

        with tempfile.TemporaryDirectory() as directory:
            summary = collect_aldi_one(
                pack, mapping, lambda _: {"items": [record]},
                Path(directory) / "feed.sqlite",
            )

        self.assertEqual(summary["status"], "observed")

    def test_retailers_table_is_seeded_idempotently(self):
        with tempfile.TemporaryDirectory() as directory:
            database = Path(directory) / "feed.sqlite"
            with closing(sqlite3.connect(database)) as connection:
                ensure_schema(connection)
                ensure_schema(connection)  # second run must be a no-op
                rows = connection.execute(
                    "SELECT retailer_slug, display_name, tier, country, data_source_type, active"
                    " FROM retailers ORDER BY retailer_slug"
                ).fetchall()

        self.assertEqual(
            rows,
            [
                ("aldi", "Aldi Ireland", 1, "IE", "scraper", 1),
                ("dunnes", "Dunnes Stores", 1, "IE", "scraper", 1),
                ("lidl", "Lidl Ireland", 1, "IE", "scraper", 1),
                ("supervalu", "SuperValu", 1, "IE", "scraper", 1),
                ("tesco", "Tesco Ireland", 1, "IE", "scraper", 1),
            ],
        )

    # --- Dunnes DRS ---

    def test_dunnes_drs_deposit_precedence_over_offer_fields(self):
        """taxDetails deposit wins; then drsDeposit/deposit/depositAmount."""
        self.assertEqual(
            _dunnes_drs_deposit({
                "deposit": "€0.99",
                "taxDetails": [{"groupName": "Deposit", "amount": "€0.15"}],
            }),
            Decimal("0.15"),
        )
        self.assertEqual(
            _dunnes_drs_deposit({
                "depositAmount": "0.99",
                "drsDeposit": "€0.25",
            }),
            Decimal("0.25"),
        )
        self.assertEqual(
            _dunnes_drs_deposit({"depositAmount": 0.15}),
            Decimal("0.15"),
        )
        # Non-deposit tax groups are ignored.
        self.assertIsNone(
            _dunnes_drs_deposit({"taxDetails": [{"groupName": "VAT", "amount": "0.23"}]})
        )
        # Live offers carry none of this evidence today.
        self.assertIsNone(_dunnes_drs_deposit({"Price": 2.49, "Tax": 0}))

    def test_dunnes_observation_records_drs_not_available_diagnostic(self):
        payload = {
            "data": {"productSearch": {"products": [{
                "productName": "Coca-Cola Zero Sugar 330ml",
                "productReference": "COKE-ZERO-330",
                "items": [{"itemId": "COKE-ZERO-330-EA", "sellers": [
                    {"commertialOffer": {"Price": "2.49", "ListPrice": "2.99", "Tax": 0}},
                ]}],
            }]}}
        }

        with tempfile.TemporaryDirectory() as directory:
            database = Path(directory) / "feed.sqlite"
            summary = collect_one(self.pack, self.mapping, lambda _: payload, database)

            self.assertEqual(summary["status"], "observed")
            with closing(sqlite3.connect(database)) as connection:
                observation = connection.execute(
                    "SELECT displayed_price, drs_deposit FROM price_observations"
                ).fetchone()
                events = connection.execute(
                    "SELECT event FROM collection_diagnostics WHERE level='warning'"
                ).fetchall()

        self.assertEqual(observation, ("2.49", None))
        self.assertIn(("drs_not_available",), events)


class ValidateListingTests(unittest.TestCase):
    def setUp(self):
        self.pack = BenchmarkPack(
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

    def test_accepts_exact_core_tokens(self):
        self.assertIsNone(_validate_listing("Coca-Cola Diet 2 Litre", self.pack))

    def test_accepts_alias_phrase(self):
        self.assertIsNone(_validate_listing("Diet Coke Soft Drink 2 Litre", self.pack))

    def test_rejects_unrelated_name(self):
        reason = _validate_listing("Sprite Zero Sugar 2 Litre", self.pack)
        self.assertIn("name mismatch", reason or "")

    def test_alias_does_not_widen_to_other_packs(self):
        stranger = BenchmarkPack(
            catalog_id="coca-zero-330-single",
            name="Coca-Cola Zero Sugar 330ml Can",
            brand="Coca-Cola",
            variant="Zero Sugar",
            pack_count=1,
            unit_size_ml=330,
            package_type="can",
            search_term="Coca-Cola Zero Sugar",
            aliases=("Coke Zero",),
        )
        self.assertIsNone(_validate_listing("Diet Coke Soft Drink 2 Litre", self.pack))
        self.assertIsNotNone(_validate_listing("Diet Coke Soft Drink 2 Litre", stranger))


if __name__ == "__main__":
    unittest.main()
