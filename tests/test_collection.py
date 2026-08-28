import builtins
import io
import json
import sqlite3
import tempfile
import types
import urllib.error
from contextlib import closing, redirect_stdout
from datetime import datetime, timedelta, timezone
from decimal import Decimal
import unittest

def _curl_cffi_available() -> bool:
    try:
        import curl_cffi  # noqa: F401
        return True
    except ImportError:
        return False
from pathlib import Path
from unittest.mock import patch

from beverage_feed.collector import (
    AldiClient,
    AldiMapping,
    BenchmarkPack,
    DunnesClient,
    DunnesMapping,
    LidlClient,
    LidlMapping,
    SuperValuClient,
    SuperValuMapping,
    TESCO_PRODUCT_QUERY,
    TescoClient,
    TescoMapping,
    _aldi_drs_deposit,
    _decimal_price,
    _decimal_text,
    _dunnes_drs_deposit,
    _lidl_drs_deposit,
    _RunLock,
    _load_mappings,
    _validate_listing,
    as_datetime,
    load_catalog,
    safe_record,
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

    def test_tesco_clubcard_multi_buy_with_attribute_records_effective_price(self):
        """CLUBCARD_PRICING-tagged multi-buys record the per-pack member price."""
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
                    "description": "Any 2 for €3.50 Clubcard Price",
                    "attributes": ["CLUBCARD_PRICING"],
                }],
            }]
        }

        with tempfile.TemporaryDirectory() as directory:
            database = Path(directory) / "feed.sqlite"
            summary = collect_tesco_one(
                self.pack, mapping, lambda _: payload, database
            )
            self.assertEqual(summary["status"], "observed")
            with closing(sqlite3.connect(database)) as connection:
                clubcard_price = connection.execute(
                    "SELECT clubcard_price FROM price_observations"
                ).fetchone()[0]

        self.assertEqual(clubcard_price, "1.75")

    def test_tesco_drs_deposit_comes_from_charges_fragment(self):
        """The structured ProductDepositReturnCharge is the primary DRS source."""
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
                "promotions": [],
                "charges": [
                    {"amount": "0.15", "__typename": "ProductDepositReturnCharge"},
                    {"__typename": "SomethingElse"},
                ],
            }]
        }

        with tempfile.TemporaryDirectory() as directory:
            database = Path(directory) / "feed.sqlite"
            summary = collect_tesco_one(
                self.pack, mapping, lambda _: payload, database
            )
            self.assertEqual(summary["status"], "observed")
            with closing(sqlite3.connect(database)) as connection:
                drs = connection.execute(
                    "SELECT drs_deposit FROM price_observations"
                ).fetchone()[0]

        self.assertEqual(drs, "0.15")

    def test_tesco_product_query_requests_clubcard_and_drs_fields(self):
        """The GraphQL query must ask for the fields the extractors read."""
        self.assertIn("attributes", TESCO_PRODUCT_QUERY)
        self.assertIn("ProductDepositReturnCharge", TESCO_PRODUCT_QUERY)
        self.assertIn("charges", TESCO_PRODUCT_QUERY)

    @unittest.skipUnless(_curl_cffi_available(), "curl-cffi not installed")
    def test_injected_opener_forces_plain_urllib_transport(self):
        """Tests (and any explicit opener) must bypass the impersonated path."""
        client = TescoClient(api_key="test-key", opener=urllib.request.build_opener())
        self.assertIsNone(client._impersonator)

    def test_uses_impersonated_transport_when_available_and_no_opener(self):
        class _FakeSession:
            def __init__(self, **kwargs):
                self.kwargs = kwargs
        real_import = builtins.__import__

        def fake_import(name, *args, **kwargs):
            if name == "curl_cffi":
                mod = types.ModuleType("curl_cffi")
                req = types.ModuleType("curl_cffi.requests")
                req.Session = _FakeSession
                mod.requests = req
                return mod
            return real_import(name, *args, **kwargs)

        builtins.__import__ = fake_import
        try:
            client = TescoClient(api_key="test-key")
            self.assertIsInstance(client._impersonator, _FakeSession)
            self.assertEqual(client._impersonator.kwargs.get("impersonate"), "chrome")
        finally:
            builtins.__import__ = real_import

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
        # Ticket 07 enforces one observation per run/retailer/pack/scope, so a
        # duplicate insert for the same cell is rejected outright.
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
                    """
                    INSERT INTO collection_results (
                        run_id, catalog_id, retailer, status, error,
                        source_product_reference, source_item_id, source_scope,
                        recorded_at
                    ) VALUES (?, ?, 'tesco', 'observed', NULL, 'tpnb', 'tpnb', NULL, ?)
                    """,
                    ("run-1", self.pack.catalog_id, "2026-01-01T00:00:00Z"),
                )
                connection.execute(
                    """
                    INSERT INTO price_observations (
                        run_id, catalog_id, retailer, source_product_reference,
                        source_item_id, source_product_name, displayed_price,
                        currency, pack_count, unit_size_ml, package_type, observed_at
                    ) VALUES ('run-1', ?, 'tesco', 'tpnb', 'tpnb', ?, '1.10', 'EUR', 1, 330, 'can', 't')
                    """,
                    (self.pack.catalog_id, self.pack.name),
                )
                with self.assertRaises(sqlite3.IntegrityError):
                    connection.execute(
                        """
                        INSERT INTO price_observations (
                            run_id, catalog_id, retailer, source_product_reference,
                            source_item_id, source_product_name, displayed_price,
                            currency, pack_count, unit_size_ml, package_type, observed_at
                        ) VALUES ('run-1', ?, 'tesco', 'tpnb', 'tpnb', ?, '1.00', 'EUR', 1, 330, 'can', 's')
                        """,
                        (self.pack.catalog_id, self.pack.name),
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

    # --- Complete retailer source handling (not_found vs source_error vs inconclusive)

    def test_dunnes_unpriced_first_seller_falls_through_to_priced_seller(self):
        payload = {
            "data": {"productSearch": {"products": [{
                "productName": "Coca-Cola Zero Sugar 330ml",
                "productReference": "COKE-ZERO-330",
                "items": [{"itemId": "COKE-ZERO-330-EA", "sellers": [
                    {"commertialOffer": {"Price": None}},
                    {"commertialOffer": {"Price": "2.49"}},
                ]}],
            }]}}
        }

        with tempfile.TemporaryDirectory() as directory:
            database = Path(directory) / "feed.sqlite"
            summary = collect_one(self.pack, self.mapping, lambda _: payload, database)

            self.assertEqual(summary["status"], "observed")
            with closing(sqlite3.connect(database)) as connection:
                observation = connection.execute(
                    "SELECT displayed_price, source_item_id FROM price_observations"
                ).fetchone()
        self.assertEqual(observation, ("2.49", "COKE-ZERO-330-EA"))

    def test_dunnes_mapped_listing_without_priced_seller_is_source_error(self):
        payload = {
            "data": {"productSearch": {"products": [{
                "productName": "Coca-Cola Zero Sugar 330ml",
                "productReference": "COKE-ZERO-330",
                "items": [{"itemId": "COKE-ZERO-330-EA", "sellers": [
                    {"commertialOffer": {"Price": None}},
                ]}],
            }]}}
        }

        with tempfile.TemporaryDirectory() as directory:
            database = Path(directory) / "feed.sqlite"
            summary = collect_one(self.pack, self.mapping, lambda _: payload, database)

            self.assertEqual(summary["status"], "source_error")
            self.assertEqual(summary["observed_count"], 0)
            with closing(sqlite3.connect(database)) as connection:
                result = connection.execute(
                    "SELECT status FROM collection_results"
                ).fetchone()
                observations = connection.execute(
                    "SELECT COUNT(*) FROM price_observations"
                ).fetchone()[0]
        self.assertEqual(result, ("source_error",))
        self.assertEqual(observations, 0)

    def test_dunnes_completeness_is_unknown_without_pagination_metadata(self):
        payload = {"data": {"productSearch": {"products": []}}}

        with tempfile.TemporaryDirectory() as directory:
            database = Path(directory) / "feed.sqlite"
            summary = collect_one(self.pack, self.mapping, lambda _: payload, database)

            self.assertEqual(summary["status"], "not_found")
            self.assertEqual(summary["complete"], "unknown")
            with closing(sqlite3.connect(database)) as connection:
                result = connection.execute(
                    "SELECT status, complete FROM collection_results"
                ).fetchone()
        self.assertEqual(result, ("not_found", "unknown"))

    def test_truncated_lidl_page_is_inconclusive_not_found(self):
        mapping = LidlMapping(
            catalog_id=self.pack.catalog_id,
            expected_product_name="Coca-Cola Zero Sugar 330ml Can",
        )
        observed_payload = {
            "items": [{
                "productId": "10062229",
                "name": "Coca-Cola Zero Sugar 330ml Can",
                "price": 2.49,
                "specialTaxes": [],
            }],
            "pagination": {"total": 1, "offset": 0},
        }
        truncated_payload = {
            "items": [{"productId": "99999", "name": "HATA Cola Drink"}],
            "pagination": {"total": 30, "offset": 0},
        }

        with tempfile.TemporaryDirectory() as directory:
            database = Path(directory) / "feed.sqlite"
            first = collect_lidl_one(self.pack, mapping, lambda _: observed_payload, database)
            self.assertEqual(first["status"], "observed")
            summary = collect_lidl_one(
                self.pack, mapping, lambda _: truncated_payload, database,
            )

            self.assertEqual(summary["status"], "inconclusive")
            self.assertEqual(summary["complete"], "false")
            with closing(sqlite3.connect(database)) as connection:
                result = connection.execute(
                    "SELECT status, complete FROM collection_results ORDER BY rowid DESC LIMIT 1"
                ).fetchone()
            # The inconclusive page must not create an observation, and the
            # older price must not resurface as current.
            self.assertEqual(result, ("inconclusive", "false"))
            self.assertEqual(len(price_history(database, retailer="lidl")), 1)
            self.assertEqual(current_feed(database, retailer="lidl"), [])

    def test_complete_lidl_page_missing_product_is_not_found(self):
        mapping = LidlMapping(
            catalog_id=self.pack.catalog_id,
            expected_product_name="Coca-Cola Zero Sugar 330ml Can",
        )
        payload = {
            "items": [{"productId": "99999", "name": "HATA Cola Drink"}],
            "pagination": {"total": 1, "offset": 0},
        }

        with tempfile.TemporaryDirectory() as directory:
            database = Path(directory) / "feed.sqlite"
            summary = collect_lidl_one(self.pack, mapping, lambda _: payload, database)

            self.assertEqual(summary["status"], "not_found")
            self.assertEqual(summary["complete"], "true")
            with closing(sqlite3.connect(database)) as connection:
                result = connection.execute(
                    "SELECT status, complete FROM collection_results"
                ).fetchone()
        self.assertEqual(result, ("not_found", "true"))

    def test_lidl_hydrated_response_without_pagination_stays_not_found(self):
        mapping = LidlMapping(
            catalog_id=self.pack.catalog_id,
            expected_product_name="Coca-Cola Zero Sugar 330ml Can",
            source_product_id="10062229",
        )

        class HydratingClient:
            def fetch_product(self, product_id):
                return {"items": [{"productId": "99999", "name": "HATA Cola Drink"}]}

            def __call__(self, term):
                raise AssertionError("direct hydration should be used")

        with tempfile.TemporaryDirectory() as directory:
            database = Path(directory) / "feed.sqlite"
            summary = collect_lidl_one(
                self.pack, mapping, HydratingClient(), database,
            )

            self.assertEqual(summary["status"], "not_found")
            self.assertEqual(summary["complete"], "unknown")

    def test_aldi_truncated_page_is_inconclusive_not_found(self):
        mapping = AldiMapping(
            catalog_id=self.pack.catalog_id,
            expected_product_name="Coca-Cola Zero Sugar 330ml Can",
        )
        payload = {
            "items": [{"productId": "000000000728654001", "brand": "COCA-COLA",
                       "name": "Zero Sugar 500ml Bottle", "price": "€2.19"}],
            "pagination": {"total": 40, "offset": 0},
        }

        with tempfile.TemporaryDirectory() as directory:
            database = Path(directory) / "feed.sqlite"
            summary = collect_aldi_one(self.pack, mapping, lambda _: payload, database)

            self.assertEqual(summary["status"], "inconclusive")
            self.assertEqual(summary["complete"], "false")
            with closing(sqlite3.connect(database)) as connection:
                result = connection.execute(
                    "SELECT status, complete FROM collection_results"
                ).fetchone()
                observations = connection.execute(
                    "SELECT COUNT(*) FROM price_observations"
                ).fetchone()[0]
        self.assertEqual(result, ("inconclusive", "false"))
        self.assertEqual(observations, 0)

    def test_run_summary_counts_inconclusive_separately(self):
        truncated_payload = {
            "items": [{"productId": "99999", "name": "HATA Cola Drink"}],
            "pagination": {"total": 30, "offset": 0},
        }

        with tempfile.TemporaryDirectory() as directory:
            database = Path(directory) / "feed.sqlite"
            summary = collect_run(
                [self.pack],
                {"lidl": [LidlMapping(
                    catalog_id=self.pack.catalog_id,
                    expected_product_name="Coca-Cola Zero Sugar 330ml Can",
                )]},
                {"lidl": lambda _: truncated_payload},
                database,
            )
            with closing(sqlite3.connect(database)) as connection:
                diagnostics = connection.execute(
                    "SELECT event, level FROM collection_diagnostics"
                ).fetchall()

        self.assertEqual(summary["status"], "completed")
        self.assertEqual(summary["inconclusive_count"], 1)
        self.assertEqual(summary["not_found_count"], 0)
        self.assertEqual(summary["failed_count"], 0)
        self.assertEqual(summary["affected_retailers"], ["lidl"])
        self.assertIn(("result", "warning"), diagnostics)

    def test_clubcard_attribution_does_not_rescue_meal_deal_promotions(self):
        mapping = TescoMapping(
            catalog_id=self.pack.catalog_id,
            expected_product_name="Coca-Cola Zero Sugar 330ml Can",
        )
        payload = {
            "products": [{
                "tpnb": "12345",
                "id": "tesco-id",
                "title": "Coca-Cola Zero Sugar 330ml Can",
                "price": {"actual": "2.99"},
                "promotions": [{
                    "description": "Meal deal main + snack + drink €3.00",
                    "attributes": ["CLUBCARD_PRICING"],
                }],
            }]
        }

        with tempfile.TemporaryDirectory() as directory:
            database = Path(directory) / "feed.sqlite"
            summary = collect_tesco_one(
                self.pack, mapping, lambda _: payload, database,
            )

            self.assertEqual(summary["status"], "observed")
            with closing(sqlite3.connect(database)) as connection:
                clubcard = connection.execute(
                    "SELECT clubcard_price FROM price_observations"
                ).fetchone()[0]
        self.assertIsNone(clubcard)


class CapturedFixtureTests(unittest.TestCase):
    """Every supported retailer ships a captured (trimmed) response fixture.

    Fixtures capture the client-normalized response shape the collectors
    consume; each was trimmed from a live captured response documented in the
    client docstrings. They pin that a realistic first page still observes a
    price and reports completeness metadata.
    """

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
        self.fixtures = Path(__file__).parent / "fixtures"

    def _fixture(self, name):
        return json.loads((self.fixtures / name).read_text())

    def test_dunnes_search_fixture_yields_observed_price(self):
        mapping = DunnesMapping(
            catalog_id=self.pack.catalog_id,
            expected_product_name="Coca-Cola Zero Sugar 330ml",
            source_product_reference="COKE-ZERO-330",
            source_item_id="COKE-ZERO-330-EA",
        )

        with tempfile.TemporaryDirectory() as directory:
            summary = collect_one(
                self.pack, mapping, lambda _: self._fixture("dunnes_search.json"),
                Path(directory) / "feed.sqlite",
            )

        self.assertEqual(summary["status"], "observed")
        self.assertEqual(summary["complete"], "unknown")

    def test_supervalu_product_fixture_yields_observed_price_and_deposit(self):
        mapping = SuperValuMapping(
            catalog_id=self.pack.catalog_id,
            expected_product_name="Coca-Cola Zero Sugar Can (330 ml)",
            source_product_id="SV-330",
        )

        with tempfile.TemporaryDirectory() as directory:
            summary = collect_supervalu_one(
                self.pack, mapping, lambda _: (_ for _ in ()).throw(
                    AssertionError("hydration should be used")
                ),
                Path(directory) / "feed.sqlite",
                store_id="store-123",
                hydrator=lambda _: self._fixture("supervalu_product.json"),
            )

        self.assertEqual(summary["status"], "observed")
        self.assertEqual(summary["source_scope"], "store-123")

    def test_tesco_products_fixture_yields_clubcard_and_deposit(self):
        mapping = TescoMapping(
            catalog_id=self.pack.catalog_id,
            expected_product_name="Coca-Cola Zero Sugar 330ml Can",
            source_tpnb="12345",
        )

        class DirectFetcher:
            def fetch_product(self, tpnb):
                fixture = json.loads(
                    (Path(__file__).parent / "fixtures" / "tesco_products.json")
                    .read_text()
                )
                return fixture

        with tempfile.TemporaryDirectory() as directory:
            summary = collect_tesco_one(
                self.pack, mapping, DirectFetcher(), Path(directory) / "feed.sqlite",
            )

        self.assertEqual(summary["status"], "observed")

    def test_lidl_search_fixture_yields_observed_price_on_complete_page(self):
        mapping = LidlMapping(
            catalog_id=self.pack.catalog_id,
            expected_product_name="Coca-Cola Zero Sugar 330ml Can",
            source_product_id="10062229",
        )

        with tempfile.TemporaryDirectory() as directory:
            summary = collect_lidl_one(
                self.pack, mapping, lambda _: self._fixture("lidl_search.json"),
                Path(directory) / "feed.sqlite",
            )

        self.assertEqual(summary["status"], "observed")
        self.assertEqual(summary["complete"], "true")

    def test_aldi_search_fixture_yields_observed_price_on_complete_page(self):
        mapping = AldiMapping(
            catalog_id=self.pack.catalog_id,
            expected_product_name="Coca-Cola Zero Sugar 330ml Can",
            source_product_id="000000000728654001",
        )

        with tempfile.TemporaryDirectory() as directory:
            summary = collect_aldi_one(
                self.pack, mapping, lambda _: self._fixture("aldi_search.json"),
                Path(directory) / "feed.sqlite",
            )

        self.assertEqual(summary["status"], "observed")
        self.assertEqual(summary["complete"], "true")


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


class _FakeHTTPResponse:
    """Minimal urlopen response: context manager with a status and a body."""

    def __init__(self, status, body):
        self.status = status
        self._body = body

    def read(self):
        return self._body

    def __enter__(self):
        return self

    def __exit__(self, *exc_info):
        return False


class DunnesClientTests(unittest.TestCase):
    """Network-boundary contract of the Dunnes storefront gateway client.

    urllib.request.urlopen is intercepted (the network seam), so no live
    endpoint is ever contacted; everything else runs the real client code.
    """

    def setUp(self):
        self.client = DunnesClient()

    def test_blank_search_term_is_rejected_before_any_request(self):
        with self.assertRaisesRegex(ValueError, "must not be empty"):
            self.client("   ")

    def test_request_targets_the_gateway_search_api(self):
        captured = {}

        def fake_urlopen(request, timeout=None):
            captured["url"] = request.full_url
            captured["headers"] = dict(request.header_items())
            return _FakeHTTPResponse(200, json.dumps({"items": []}).encode())

        with patch("urllib.request.urlopen", fake_urlopen):
            self.client("Coca-Cola Zero")
        self.assertIn("q=Coca-Cola+Zero", captured["url"])
        self.assertIn("take=50", captured["url"])
        headers = {k.lower(): v for k, v in captured["headers"].items()}
        self.assertEqual(headers.get("accept"), "application/json")

    def test_http_error_status_raises_a_runtime_error(self):
        with patch(
            "urllib.request.urlopen",
            lambda request, timeout=None: _FakeHTTPResponse(503, b"{}"),
        ):
            with self.assertRaisesRegex(RuntimeError, "Dunnes HTTP 503"):
                self.client("Coca-Cola Zero")

    def test_connection_failure_is_wrapped_and_chained(self):
        error = urllib.error.URLError("connection refused")

        def fake_urlopen(request, timeout=None):
            raise error

        with patch("urllib.request.urlopen", fake_urlopen):
            with self.assertRaises(RuntimeError) as ctx:
                self.client("Coca-Cola Zero")
        self.assertIn("Dunnes request failed", str(ctx.exception))
        self.assertIs(ctx.exception.__cause__, error)

    def test_payload_without_an_items_list_is_a_source_error(self):
        with patch(
            "urllib.request.urlopen",
            lambda request, timeout=None: _FakeHTTPResponse(200, b'{"foo": 1}'),
        ):
            with self.assertRaisesRegex(RuntimeError, "no items list"):
                self.client("Coca-Cola Zero")

    def test_items_are_normalised_into_the_vtex_search_envelope(self):
        payload = {
            "items": [
                "not-a-dict",
                {
                    "productId": "p-1",
                    "sku": "s-1",
                    "name": "Coca-Cola Zero Sugar 330ml",
                    "priceNumeric": 2.79,
                    "wasPriceNumeric": 3.19,
                    "taxDetails": {"deposit": "0.15"},
                },
            ]
        }
        with patch(
            "urllib.request.urlopen",
            lambda request, timeout=None: _FakeHTTPResponse(
                200, json.dumps(payload).encode()
            ),
        ):
            result = self.client("Coca-Cola Zero")
        products = result["data"]["productSearch"]["products"]
        self.assertEqual(len(products), 1)  # non-dict entries skipped
        product = products[0]
        self.assertEqual(product["productName"], "Coca-Cola Zero Sugar 330ml")
        self.assertEqual(product["productReference"], "p-1")
        offer = product["items"][0]["sellers"][0]["commertialOffer"]
        self.assertEqual(offer["Price"], 2.79)
        self.assertEqual(offer["ListPrice"], 3.19)
        self.assertEqual(offer["taxDetails"], {"deposit": "0.15"})


class SuperValuHydrationTests(unittest.TestCase):
    """Product-ID hydration contract of the SuperValu client."""

    def _client(self, payload):
        client = SuperValuClient("store-123")
        patcher = patch.object(client, "_get", lambda url, **kwargs: payload)
        patcher.start()
        self.addCleanup(patcher.stop)
        return client

    def test_blank_product_id_is_rejected_before_any_request(self):
        client = self._client({})
        with self.assertRaisesRegex(ValueError, "must not be empty"):
            client.fetch_product("  ")

    def test_sku_is_promoted_to_product_id_and_price_is_normalised(self):
        client = self._client({"sku": "S-100", "price": "\u20ac1.40"})
        payload = client.fetch_product("S-100")
        self.assertEqual(payload["productId"], "S-100")
        self.assertEqual(payload["priceNumeric"], 1.4)

    def test_existing_product_id_and_numeric_price_are_left_alone(self):
        client = self._client({"productId": "P-1", "priceNumeric": 2.2, "price": "x"})
        payload = client.fetch_product("P-1")
        self.assertEqual(payload["productId"], "P-1")
        self.assertEqual(payload["priceNumeric"], 2.2)

    def test_malformed_price_never_raises_and_stays_unparsed(self):
        client = self._client({"sku": "S-1", "price": "currently unavailable"})
        payload = client.fetch_product("S-1")
        self.assertEqual(payload["productId"], "S-1")
        self.assertNotIn("priceNumeric", payload)


class CollectRunRetailerDispatchTests(unittest.TestCase):
    """collect_run routes each retailer to its dedicated collector."""

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

    def test_lidl_and_aldi_mappings_dispatch_to_their_collectors(self):
        lidl_record = {
            "productId": "10062229",
            "name": "Coca-Cola Zero Sugar 330ml Can",
            "price": 2.49,
            "basePriceText": "\u20ac0.15 Deposit Return",
            "specialTaxes": [],
        }
        aldi_record = {
            "productId": "000000000728654001",
            "name": "Coca-Cola Zero Sugar 330ml Can",
            "brand": "COCA-COLA",
            "price": "\u20ac2.49",
            "bottleDepositText": "\u20ac0.15",
        }
        with tempfile.TemporaryDirectory() as directory:
            database = Path(directory) / "feed.sqlite"
            summary = collect_run(
                [self.pack],
                {
                    "lidl": [LidlMapping(
                        catalog_id=self.pack.catalog_id,
                        expected_product_name="Coca-Cola Zero Sugar 330ml Can",
                    )],
                    "aldi": [AldiMapping(
                        catalog_id=self.pack.catalog_id,
                        expected_product_name="Coca-Cola Zero Sugar 330ml Can",
                    )],
                },
                {
                    "lidl": lambda _: {"items": [lidl_record]},
                    "aldi": lambda _: {"items": [aldi_record]},
                },
                database,
            )
            with closing(sqlite3.connect(database)) as connection:
                observations = connection.execute(
                    "SELECT retailer, displayed_price FROM price_observations "
                    "ORDER BY retailer"
                ).fetchall()
                results = connection.execute(
                    "SELECT retailer, status FROM collection_results ORDER BY retailer"
                ).fetchall()

        self.assertEqual(summary["status"], "completed")
        self.assertEqual(summary["observed_count"], 2)
        self.assertEqual(observations, [("aldi", "2.49"), ("lidl", "2.49")])
        self.assertEqual(results, [("aldi", "observed"), ("lidl", "observed")])

    def test_unknown_retailer_is_isolated_as_a_source_error(self):
        # A pair with an unsupported retailer must not crash the run; it is
        # recorded as an isolated source_error for diagnostics instead.
        with tempfile.TemporaryDirectory() as directory:
            database = Path(directory) / "feed.sqlite"
            summary = collect_run(
                [self.pack],
                {"kruidvat": [DunnesMapping(
                    catalog_id=self.pack.catalog_id,
                    expected_product_name="Coca-Cola Zero Sugar 330ml Can",
                )]},
                {"kruidvat": lambda _: {}},
                database,
            )
            with closing(sqlite3.connect(database)) as connection:
                result = connection.execute(
                    "SELECT status, error FROM collection_results"
                ).fetchone()
                observations = connection.execute(
                    "SELECT COUNT(*) FROM price_observations"
                ).fetchone()[0]

        self.assertEqual(summary["status"], "failed")
        self.assertEqual(summary["failed_count"], 1)
        self.assertEqual(result[0], "source_error")
        self.assertIn("unsupported retailer adapter", result[1])
        # A failed pair never writes a Price Observation.
        self.assertEqual(observations, 0)


class CollectionRunRecoveryTests(unittest.TestCase):
    """Runs are recoverable, locked, and idempotent (audit ticket 07).

    An interrupted run gets an explicit terminal status, concurrent local
    collectors are excluded by a run lock, and a retailer-pack cell yields at
    most one observation per run/source scope — with previous observations
    preserved after failed runs.
    """

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
        self.payload = {
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

    def _run_rows(self, database):
        with closing(sqlite3.connect(database)) as connection:
            return connection.execute(
                "SELECT run_id, status, finished_at FROM collection_runs ORDER BY rowid"
            ).fetchall()

    def _collect(self, database, fetcher):
        return collect_run(
            [self.pack],
            {"dunnes": [self.mapping]},
            {"dunnes": fetcher},
            database,
        )

    def test_stale_running_run_from_a_crashed_process_is_finalized_as_interrupted(self):
        # A collector that died mid-run leaves a 'running' row behind; the next
        # run must finalize it with an explicit terminal status.
        with tempfile.TemporaryDirectory() as directory:
            database = Path(directory) / "feed.sqlite"
            with closing(sqlite3.connect(database)) as connection:
                ensure_schema(connection)
                connection.execute(
                    "INSERT INTO collection_runs VALUES (?, ?, ?, ?, ?, ?, ?)",
                    (
                        "crashed-run", "2026-01-01T00:00:00Z", "2026-01-01T00:00:00Z",
                        "running", 0, 0, '{"status": "running"}',
                    ),
                )
                connection.commit()
            summary = self._collect(database, lambda _: self.payload)
            rows = self._run_rows(database)

        self.assertEqual(summary["status"], "completed")
        self.assertEqual([row[1] for row in rows], ["interrupted", "completed"])
        self.assertTrue(all(row[2] for row in rows))

    def test_keyboard_interrupt_finalizes_the_run_as_interrupted_not_running(self):
        def interrupted(_):
            raise KeyboardInterrupt()

        with tempfile.TemporaryDirectory() as directory:
            database = Path(directory) / "feed.sqlite"
            with self.assertRaises(KeyboardInterrupt):
                self._collect(database, interrupted)
            rows = self._run_rows(database)

        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0][1], "interrupted")
        self.assertTrue(rows[0][2])

    def test_sqlite_failure_finalizes_the_run_and_releases_the_lock(self):
        with tempfile.TemporaryDirectory() as directory:
            database = Path(directory) / "feed.sqlite"
            with patch(
                "beverage_feed.collector._log_decision",
                side_effect=sqlite3.OperationalError("database is locked"),
            ):
                with self.assertRaises(sqlite3.OperationalError):
                    self._collect(database, lambda _: self.payload)
            rows = self._run_rows(database)
            # The lock is released after the failure, so a retry succeeds.
            retry = self._collect(database, lambda _: self.payload)

        self.assertEqual(rows[0][1], "failed")
        self.assertTrue(rows[0][2])
        self.assertEqual(retry["status"], "completed")

    def test_concurrent_collector_is_blocked_and_duplicate_sequential_run_is_allowed(self):
        with tempfile.TemporaryDirectory() as directory:
            database = Path(directory) / "feed.sqlite"
            with _RunLock(database):
                with self.assertRaises(RuntimeError) as raised:
                    self._collect(database, lambda _: self.payload)
            self.assertIn("lock", str(raised.exception))
            # A second invocation after the lock is released is not an error.
            summary = self._collect(database, lambda _: self.payload)
            second = self._collect(database, lambda _: self.payload)

        self.assertEqual(summary["status"], "completed")
        self.assertEqual(second["status"], "completed")

    def test_repeated_processing_of_the_same_cell_is_idempotent(self):
        # Processing the same retailer-pack cell twice within one run leaves
        # exactly one observation and one collection result.
        with tempfile.TemporaryDirectory() as directory:
            database = Path(directory) / "feed.sqlite"
            with closing(sqlite3.connect(database)) as connection:
                ensure_schema(connection)
                connection.execute(
                    "INSERT INTO collection_runs VALUES (?, ?, ?, ?, ?, ?, ?)",
                    ("run-fixed", "t", "t", "running", 0, 0, "{}"),
                )
                connection.commit()
            collect_one(
                self.pack, self.mapping, lambda _: self.payload, database,
                _run_id="run-fixed",
            )
            collect_one(
                self.pack, self.mapping, lambda _: self.payload, database,
                _run_id="run-fixed",
            )
            with closing(sqlite3.connect(database)) as connection:
                observations = connection.execute(
                    "SELECT COUNT(*) FROM price_observations"
                ).fetchone()[0]
                results = connection.execute(
                    "SELECT COUNT(*) FROM collection_results"
                ).fetchone()[0]

        self.assertEqual(observations, 1)
        self.assertEqual(results, 1)

    def test_observation_uniqueness_is_enforced_per_run_cell_and_scope(self):
        # One observation per run, retailer, Catalog Pack, and source scope;
        # a different source scope is a distinct observation.
        insert = """
            INSERT INTO price_observations (
                run_id, catalog_id, retailer, source_product_reference,
                source_item_id, source_product_name, displayed_price, source_scope,
                currency, pack_count, unit_size_ml, package_type, observed_at
            ) VALUES ('run-1', ?, 'dunnes', 'ref', 'item', 'Coke', '2.49', ?,
                      'EUR', 1, 330, 'can', 't')
            """
        with tempfile.TemporaryDirectory() as directory:
            database = Path(directory) / "feed.sqlite"
            with closing(sqlite3.connect(database)) as connection:
                ensure_schema(connection)
                connection.execute(
                    "INSERT INTO collection_runs VALUES (?, ?, ?, ?, ?, ?, ?)",
                    ("run-1", "t", "t", "completed", 1, 0, "{}"),
                )
                connection.execute(insert, (self.pack.catalog_id, None))
                connection.commit()
            with closing(sqlite3.connect(database)) as connection:
                with self.assertRaises(sqlite3.IntegrityError):
                    connection.execute(insert, (self.pack.catalog_id, None))
                connection.execute(insert, (self.pack.catalog_id, "store-042"))
                connection.commit()
                counts = connection.execute(
                    "SELECT COALESCE(source_scope, ''), COUNT(*) "
                    "FROM price_observations GROUP BY 1"
                ).fetchall()

        self.assertEqual(sorted(counts), [("", 1), ("store-042", 1)])

    def test_failed_run_preserves_previous_observations_and_current_feed(self):
        def broken(_):
            raise RuntimeError("retailer outage")

        with tempfile.TemporaryDirectory() as directory:
            database = Path(directory) / "feed.sqlite"
            first = self._collect(database, lambda _: self.payload)
            second = self._collect(database, broken)
            with closing(sqlite3.connect(database)) as connection:
                observations = connection.execute(
                    "SELECT COUNT(*) FROM price_observations"
                ).fetchone()[0]
            feed = current_feed(database)
            history = price_history(database)

        self.assertEqual(first["status"], "completed")
        self.assertEqual(second["status"], "failed")
        # The failed run never deletes or overwrites the earlier observation.
        self.assertEqual(observations, 1)
        # The pair's latest result is a source_error, so the stale price is not
        # presented as current; history and last-seen keep it for reference.
        self.assertEqual(feed, [])
        self.assertEqual(len(history), 1)


class MoneyParsingTests(unittest.TestCase):
    """Money is Decimal, ROUND_HALF_UP, euro-tolerant, never negative."""

    def test_accepts_euro_sign_whitespace_and_comma_decimal_marker(self):
        for raw, expected in [("€2.49", "2.49"), (" 2.49 ", "2.49"), ("2,49", "2.49")]:
            with self.subTest(raw=raw):
                self.assertEqual(_decimal_price(raw), Decimal(expected))

    def test_mixed_separators_parse_by_the_last_decimal_marker(self):
        # Continental "1.234,56" and anglo "1,234.56" must both mean 1234.56.
        self.assertEqual(_decimal_price("1.234,56"), Decimal("1234.56"))
        self.assertEqual(_decimal_price("1,234.56"), Decimal("1234.56"))

    def test_rounding_is_half_up_to_cents(self):
        self.assertEqual(_decimal_price("2.675"), Decimal("2.68"))
        self.assertEqual(_decimal_text(Decimal("2.675")), "2.68")

    def test_missing_price_is_rejected(self):
        with self.assertRaises(ValueError):
            _decimal_price(None)

    def test_malformed_price_is_rejected(self):
        with self.assertRaises(ValueError):
            _decimal_price("not-a-price")

    def test_negative_price_is_rejected(self):
        with self.assertRaises(ValueError):
            _decimal_price("-1.00")


class SecretRedactionTests(unittest.TestCase):
    """Raw records are persisted only after secret scrubbing (CONTRIBUTING §6)."""

    def test_sensitive_keys_are_redacted_at_any_depth(self):
        record = {
            "authorization": "Bearer abc",
            "nested": [
                {"apiKey": "k-123", "safe": "value"},
                {"deep": {"X-Auth-Token": "t-9", "cookie": "c"}},
            ],
            "password": "p",
        }
        scrubbed = json.loads(safe_record(record) or "{}")

        self.assertEqual(scrubbed["authorization"], "[redacted]")
        self.assertEqual(scrubbed["password"], "[redacted]")
        self.assertEqual(scrubbed["nested"][0]["apiKey"], "[redacted]")
        self.assertEqual(scrubbed["nested"][0]["safe"], "value")
        self.assertEqual(scrubbed["nested"][1]["deep"]["X-Auth-Token"], "[redacted]")
        self.assertEqual(scrubbed["nested"][1]["deep"]["cookie"], "[redacted]")

    def test_none_record_stays_none(self):
        self.assertIsNone(safe_record(None))

    def test_unserialisable_record_degrades_to_its_string_form(self):
        record = {"price": Decimal("2.49")}  # Decimal is handled via default=str
        self.assertIn("2.49", safe_record(record))


class TimeNormalisationTests(unittest.TestCase):
    """Timestamps rehydrate as aware UTC (CONTRIBUTING §5)."""

    def test_none_rehydrates_to_the_current_utc_moment(self):
        rehydrated = as_datetime(None)
        self.assertEqual(rehydrated.tzinfo, timezone.utc)
        self.assertLess(abs(rehydrated - datetime.now(timezone.utc)), timedelta(seconds=5))

    def test_naive_datetime_is_interpreted_as_utc(self):
        rehydrated = as_datetime(datetime(2026, 8, 27, 12, 0, 0))
        self.assertEqual(rehydrated, datetime(2026, 8, 27, 12, 0, 0, tzinfo=timezone.utc))

    def test_aware_datetime_passes_through_unchanged(self):
        moment = datetime(2026, 8, 27, 12, 0, 0, tzinfo=timezone.utc)
        self.assertEqual(as_datetime(moment), moment)

    def test_z_suffix_and_naive_strings_rehydrate_as_utc(self):
        for text in ["2026-08-27T12:00:00Z", "2026-08-27T12:00:00"]:
            with self.subTest(text=text):
                self.assertEqual(
                    as_datetime(text),
                    datetime(2026, 8, 27, 12, 0, 0, tzinfo=timezone.utc),
                )


class TescoClientContractTests(unittest.TestCase):
    """Malformed gateway responses must degrade to source_error, not crash."""

    class _Response(io.BytesIO):
        status = 200

        def __enter__(self):
            return self

        def __exit__(self, *args):
            self.close()

    @staticmethod
    def _opener(responses):
        class Opener:
            def open(self, request, timeout):
                return TescoClientContractTests._Response(responses.pop(0))

        return Opener()

    def _client(self, *responses):
        return TescoClient(
            api_key="test-key",
            opener=self._opener([json.dumps(body).encode() for body in responses]),
            min_request_interval=0,
        )

    def test_blank_search_term_is_rejected_before_any_request(self):
        client = self._client({})
        with self.assertRaises(ValueError):
            client("   ")

    def test_blank_tpnb_is_rejected_before_any_request(self):
        client = self._client({})
        with self.assertRaises(ValueError):
            client.fetch_product("  ")

    def test_search_payload_without_results_is_a_source_error(self):
        client = self._client({"unexpected": {}})
        with self.assertRaises(RuntimeError):
            client("Coca-Cola")

    def test_search_with_no_matches_skips_graphql_hydration(self):
        client = self._client({"ie": {"ghs": {"products": {"results": []}}}})
        payload = client("Coca-Cola")
        self.assertEqual(payload["products"], [])

    def test_graphql_error_envelope_is_a_source_error(self):
        client = self._client(
            {"ie": {"ghs": {"products": {"results": [{"tpnb": 12345}]}}}},
            [{"errors": [{"message": "unknown tpnb"}]}],
        )
        with self.assertRaises(RuntimeError) as caught:
            client("Coca-Cola")
        self.assertIn("unknown tpnb", str(caught.exception))

    def test_graphql_response_that_is_not_a_list_is_a_source_error(self):
        client = self._client(
            {"ie": {"ghs": {"products": {"results": [{"tpnb": 12345}]}}}},
            {"data": "unexpected"},
        )
        with self.assertRaises(RuntimeError):
            client("Coca-Cola")


class TescoEvidencePrecedenceTests(unittest.TestCase):
    """Loyalty and DRS evidence: explicit fields beat promotion text."""

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
        self.mapping = TescoMapping(
            catalog_id=self.pack.catalog_id,
            expected_product_name="Coca-Cola Zero Sugar 330ml Can",
            source_tpnb="12345",
        )

    def _collect(self, item):
        with tempfile.TemporaryDirectory() as directory:
            database = Path(directory) / "feed.sqlite"
            summary = collect_tesco_one(
                self.pack, self.mapping, lambda _: {"products": [item]}, database
            )
            self.assertEqual(summary["status"], "observed", summary.get("error"))
            with closing(sqlite3.connect(database)) as connection:
                return connection.execute(
                    "SELECT clubcard_price, drs_deposit FROM price_observations"
                ).fetchone()

    def _item(self, **extra):
        item = {
            "tpnb": "12345",
            "id": "tesco-id",
            "title": "Coca-Cola Zero Sugar 330ml Can",
            "price": {"actual": "2.49"},
            "promotions": [],
        }
        item.update(extra)
        return item

    def test_explicit_loyalty_field_beats_promotion_text(self):
        clubcard, _ = self._collect(self._item(
            clubcardPrice="2.19",
            promotions=[{
                "description": "Any 2 for €3.50 Clubcard Price",
                "attributes": ["CLUBCARD_PRICING"],
            }],
        ))
        self.assertEqual(clubcard, "2.19")

    def test_zero_quantity_multi_buy_is_not_a_loyalty_price(self):
        clubcard, _ = self._collect(self._item(
            promotions=[{
                "description": "Any 0 for €3.50 Clubcard Price",
                "attributes": ["CLUBCARD_PRICING"],
            }],
        ))
        self.assertIsNone(clubcard)

    def test_top_level_drs_deposit_is_the_last_fallback(self):
        _, drs = self._collect(self._item(drsDeposit="0.25"))
        self.assertEqual(drs, "0.25")

    def test_response_without_a_products_list_is_a_source_error(self):
        with tempfile.TemporaryDirectory() as directory:
            database = Path(directory) / "feed.sqlite"
            summary = collect_tesco_one(
                self.pack, self.mapping, lambda _: {"nope": []}, database
            )
            observations = sqlite3.connect(database).execute(
                "SELECT COUNT(*) FROM price_observations"
            ).fetchone()[0]

        self.assertEqual(summary["status"], "source_error")
        self.assertEqual(observations, 0)


class RetentionAndInputValidationTests(unittest.TestCase):
    """Config-file and retention guards fail loudly on malformed input."""

    def test_retention_periods_must_be_ordered(self):
        with tempfile.TemporaryDirectory() as directory:
            database = Path(directory) / "feed.sqlite"
            with closing(sqlite3.connect(database)) as connection:
                ensure_schema(connection)
            with self.assertRaises(ValueError):
                purge_retention(database, raw_days=200, dormant_days=100)

    def test_catalog_file_must_be_a_list_of_objects(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "catalog.json"
            path.write_text(json.dumps({"catalog_id": "x"}))
            with self.assertRaises(ValueError):
                load_catalog(path)

            path.write_text(json.dumps(["not-an-object"]))
            with self.assertRaises(ValueError):
                load_catalog(path)

    def test_mapping_file_rejects_unsupported_retailer_and_non_list_rows(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "mappings.json"
            path.write_text(json.dumps({"kruidvat": []}))
            with self.assertRaises(ValueError):
                _load_mappings(path)

            path.write_text(json.dumps({"dunnes": {"catalog_id": "x"}}))
            with self.assertRaises(ValueError):
                _load_mappings(path)

    def test_legacy_list_mapping_file_is_treated_as_dunnes(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "mappings.json"
            path.write_text(json.dumps([{
                "catalog_id": "coke-zero-330-single",
                "expected_product_name": "Coca-Cola Zero Sugar 330ml",
                "status": "approved",
            }]))
            mappings = _load_mappings(path)

        self.assertEqual(set(mappings), {"dunnes"})
        self.assertEqual(mappings["dunnes"][0].catalog_id, "coke-zero-330-single")


class CollectionCliGuardTests(unittest.TestCase):
    """The collection CLI must refuse to run on misconfiguration."""

    def setUp(self):
        self.pack_row = {
            "catalog_id": "coke-zero-330-single",
            "name": "Coca-Cola Zero Sugar 330ml Can",
            "brand": "Coca-Cola",
            "variant": "Zero Sugar",
            "pack_count": 1,
            "unit_size_ml": 330,
            "package_type": "can",
            "search_term": "Coca-Cola Zero Sugar 330ml",
            "aliases": [],
        }

    def _write_inputs(self, root, mappings):
        catalog_path = root / "catalog.json"
        mapping_path = root / "mappings.json"
        catalog_path.write_text(json.dumps([self.pack_row]))
        mapping_path.write_text(json.dumps(mappings))
        return catalog_path, mapping_path

    def test_unknown_catalog_id_is_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            catalog_path, mapping_path = self._write_inputs(root, {"dunnes": []})
            with self.assertRaises(ValueError) as caught:
                main([
                    "--catalog", str(catalog_path),
                    "--mapping", str(mapping_path),
                    "--database", str(root / "feed.sqlite"),
                    "--catalog-id", "does-not-exist",
                ])
        self.assertIn("catalog pack not found", str(caught.exception))

    def test_supervalu_without_a_store_id_fails_configuration(self):
        mappings = {"supervalu": [{
            "catalog_id": "coke-zero-330-single",
            "expected_product_name": "Coca-Cola Zero Sugar Can (330 ml)",
            "source_product_id": "SV-330",
            "status": "approved",
        }]}
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            catalog_path, mapping_path = self._write_inputs(root, mappings)
            with patch.dict("os.environ", {"SUPERVALU_STORE_ID": ""}), \
                    self.assertRaises(SystemExit) as caught:
                main([
                    "--catalog", str(catalog_path),
                    "--mapping", str(mapping_path),
                    "--database", str(root / "feed.sqlite"),
                ])
        self.assertEqual(caught.exception.code, 2)

    def test_unconfigured_retailer_is_skipped_and_a_lone_one_aborts(self):
        # TESCO_API_KEY deliberately absent: the retailer is skipped with a
        # note, and with no other retailer left the run cannot start.
        mappings = {"tesco": [{
            "catalog_id": "coke-zero-330-single",
            "expected_product_name": "Coca-Cola Zero Sugar 330ml Can",
            "source_tpnb": "12345",
            "status": "approved",
        }]}
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            catalog_path, mapping_path = self._write_inputs(root, mappings)
            with patch.dict("os.environ", {"TESCO_API_KEY": ""}), \
                    self.assertRaises(ValueError) as caught:
                main([
                    "--catalog", str(catalog_path),
                    "--mapping", str(mapping_path),
                    "--database", str(root / "feed.sqlite"),
                ])
        self.assertIn("no collectable retailers", str(caught.exception))


if __name__ == "__main__":
    unittest.main()
