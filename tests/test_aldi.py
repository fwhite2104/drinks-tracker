"""Tests for the Aldi IE Glue collection client (full-feed-coverage #10).

Fixtures are trimmed recordings of the real API shape — the single-SKU priced
response and search results captured during research ticket 03 and live probes
against ``asl.api.aldi.ie`` (see
``.scratch/full-feed-coverage/research/aldi/FINDINGS.md``).  Tests never call
live Aldi endpoints.
"""

from __future__ import annotations

import io
import json
import sqlite3
import tempfile
import urllib.parse
from contextlib import closing
from decimal import Decimal
from pathlib import Path
import unittest

from beverage_feed.aldi import (
    ALDI_SEARCH_LIMIT,
    AldiClient,
    parse_selling_size,
)
from beverage_feed.collector import AldiMapping, BenchmarkPack, collect_aldi_one


def _priced_apple_juice() -> dict:
    """Trimmed recording of GET /commerce/products/{sku}?servicePoint=D001…."""
    return {
        "sku": "000000000000399029",
        "name": "Pure Pressed Apple Juice",
        "brandName": "THE JUICE COMPANY",
        "sellingSize": "1 L",
        "price": {
            "amount": 149,
            "amountRelevant": 149,
            "amountRelevantDisplay": "\u20ac1.49",
            "bottleDeposit": 0,
            "bottleDepositDisplay": "\u20ac0.00",
            "comparison": 149,
            "comparisonDisplay": "\u20ac1.49/1 L",
            "currencyCode": "EUR",
            "currencySymbol": "\u20ac",
            "wasPriceDisplay": None,
        },
    }


def _search_page() -> dict:
    """Trimmed recording of GET /commerce/v3/product-search?q=apple+juice…."""
    return {
        "meta": {
            "pagination": {"offset": 0, "limit": 30, "totalCount": 70},
        },
        "data": [
            {
                "sku": "000000000000568469",
                "name": "Pressed Apple Juice",
                "brandName": "THE JUICE COMPANY",
                "sellingSize": "1.75 L",
                "price": {
                    "amount": 249,
                    "amountRelevantDisplay": "\u20ac2.49",
                    "bottleDeposit": 0,
                    "comparison": 142,
                    "comparisonDisplay": "\u20ac1.42/1 L",
                    "wasPriceDisplay": "\u20ac2.99",
                },
            },
            dict(_priced_apple_juice()),
        ],
    }


class _RecordingOpener:
    """Urllib opener substitute serving queued JSON bodies."""

    def __init__(self, payloads: list[str | bytes]):
        self._payloads = list(payloads)
        self.requests: list[urllib.request.Request] = []

    def open(self, request: urllib.request.Request, timeout: float) -> io.BytesIO:
        self.requests.append(request)

        class Enterable(io.BytesIO):
            status = 200

            def __enter__(self) -> "Enterable":
                return self

            def __exit__(self, *args: object) -> None:
                self.close()

        body = self._payloads.pop(0)
        return Enterable(body.encode() if isinstance(body, str) else body)


class SellingSizeParsingTests(unittest.TestCase):
    """sellingSize strings observed live, parsed into pack evidence."""

    def test_volume_strings_parse_to_unit_size(self):
        self.assertEqual(parse_selling_size("1 L"), (1, 1000))
        self.assertEqual(parse_selling_size("1.75 L"), (1, 1750))
        self.assertEqual(parse_selling_size("0.07 L"), (1, 70))
        self.assertEqual(parse_selling_size("330 ML"), (1, 330))
        self.assertEqual(parse_selling_size("500 ml"), (1, 500))

    def test_explicit_multipack_prefix_gives_pack_count(self):
        self.assertEqual(parse_selling_size("6 x 330 ml"), (6, 330))
        self.assertEqual(parse_selling_size("4X1.5L"), (4, 1500))

    def test_non_volume_units_return_none(self):
        # Live strings for non-drinks (and weight-based) listings must not
        # invent a volume.
        self.assertIsNone(parse_selling_size("6 Pack"))
        self.assertIsNone(parse_selling_size("1 Each"))
        self.assertIsNone(parse_selling_size("0.15 KG"))
        self.assertIsNone(parse_selling_size("0.25 Kg drained"))
        self.assertIsNone(parse_selling_size(""))
        self.assertIsNone(parse_selling_size(None))


class AldiClientSearchTests(unittest.TestCase):
    def test_searches_and_normalizes_priced_records(self):
        opener = _RecordingOpener([json.dumps(_search_page())])
        client = AldiClient(opener=opener, min_request_interval=0)

        payload = client("apple juice")

        self.assertEqual(
            payload["pagination"], {"total": 70, "offset": 0}
        )
        first = payload["items"][0]
        self.assertEqual(first["productId"], "000000000000568469")
        self.assertEqual(first["name"], "Pressed Apple Juice")
        self.assertEqual(first["brand"], "THE JUICE COMPANY")
        self.assertEqual(first["price"], "\u20ac2.49")
        self.assertEqual(first["oldPrice"], "\u20ac2.99")
        self.assertEqual(first["unitPriceText"], "\u20ac1.42/1 L")
        self.assertEqual(first["totalVolume"], "1.75 L")
        # "1.75 L" carries no explicit pack count: total-volume evidence only.
        self.assertEqual(first["packCount"], 1)
        self.assertEqual(first["unitSizeMl"], 1750)
        self.assertNotIn("bottleDepositText", first)  # zero deposit = no deposit
        self.assertEqual(payload["items"][1]["unitSizeMl"], 1000)

        url = opener.requests[0].full_url
        self.assertIn("https://asl.api.aldi.ie/commerce/v3/product-search?", url)
        self.assertIn("q=apple+juice", url)
        self.assertIn(f"limit={ALDI_SEARCH_LIMIT}", url)
        self.assertIn("servicePoint=D001", url)
        self.assertIn("serviceType=walk-in", url)
        self.assertEqual(
            opener.requests[0].get_header("User-agent"), "drinks-tracker/0.1"
        )

    def test_explicit_multipack_selling_size_yields_pack_evidence(self):
        page = _search_page()
        page["data"][0]["sellingSize"] = "6 x 330 ml"
        opener = _RecordingOpener([json.dumps(page)])
        client = AldiClient(opener=opener, min_request_interval=0)

        record = client("cola")["items"][0]

        self.assertEqual(record["packCount"], 6)
        self.assertEqual(record["unitSizeMl"], 330)

    def test_derives_euro_price_from_cents_without_display_string(self):
        page = _search_page()
        del page["data"][0]["price"]["amountRelevantDisplay"]
        opener = _RecordingOpener([json.dumps(page)])
        client = AldiClient(opener=opener, min_request_interval=0)

        record = client("apple juice")["items"][0]

        self.assertEqual(record["price"], "\u20ac2.49")

    def test_unparseable_selling_size_omits_pack_evidence(self):
        page = _search_page()
        page["data"][0]["sellingSize"] = "6 Each"
        opener = _RecordingOpener([json.dumps(page)])
        client = AldiClient(opener=opener, min_request_interval=0)

        record = client("apple juice")["items"][0]

        self.assertEqual(record["totalVolume"], "6 Each")
        self.assertNotIn("packCount", record)
        self.assertNotIn("unitSizeMl", record)

    def test_empty_search_term_is_rejected(self):
        client = AldiClient(opener=_RecordingOpener([]), min_request_interval=0)
        with self.assertRaises(ValueError):
            client("   ")

    def test_malformed_search_response_raises_source_error(self):
        client = AldiClient(
            opener=_RecordingOpener([json.dumps({"errors": []})]),
            min_request_interval=0,
        )
        with self.assertRaises(RuntimeError) as context:
            client("apple juice")
        self.assertIn("no data list", str(context.exception))

    def test_network_failure_raises_useful_error(self):
        class BrokenOpener:
            def open(self, request: urllib.request.Request, timeout: float) -> io.BytesIO:
                raise OSError("connection refused")

        client = AldiClient(opener=BrokenOpener(), min_request_interval=0)
        with self.assertRaises(RuntimeError) as context:
            client("apple juice")
        self.assertIn("Aldi request failed", str(context.exception))


class AldiClientHydrationTests(unittest.TestCase):
    def test_fetches_priced_product_by_sku(self):
        page = {
            "meta": {"pagination": {"offset": 0, "limit": 6, "totalCount": 1}},
            "data": [_priced_apple_juice()],
        }
        opener = _RecordingOpener([json.dumps(page)])
        client = AldiClient(opener=opener, min_request_interval=0)

        payload = client.fetch_product("000000000000399029")

        record = payload["items"][0]
        self.assertEqual(record["productId"], "000000000000399029")
        self.assertEqual(record["price"], "\u20ac1.49")
        self.assertEqual(record["unitPriceText"], "\u20ac1.49/1 L")
        url = opener.requests[0].full_url
        self.assertIn("/commerce/v2/products?", url)
        self.assertIn("skus=000000000000399029", urllib.parse.unquote_plus(url))
        self.assertIn("servicePoint=D001", url)

    def test_missing_sku_returns_empty_items(self):
        page = {"meta": {"pagination": {}}, "data": [_priced_apple_juice()]}
        client = AldiClient(
            opener=_RecordingOpener([json.dumps(page)]), min_request_interval=0
        )

        self.assertEqual(client.fetch_product("999999999999999"), {"items": []})

    def test_empty_product_id_is_rejected(self):
        client = AldiClient(opener=_RecordingOpener([]), min_request_interval=0)
        with self.assertRaises(ValueError):
            client.fetch_product("  ")

    def test_empty_service_point_is_rejected(self):
        with self.assertRaises(ValueError):
            AldiClient(service_point="  ")


class AldiCollectionIntegrationTests(unittest.TestCase):
    """The registered client satisfies the collect_aldi_one fetcher contract."""

    def setUp(self) -> None:
        self.pack = BenchmarkPack(
            catalog_id="juice-company-apple-juice-1l",
            name="The Juice Company Pure Pressed Apple Juice 1L",
            brand="The Juice Company",
            variant="Pure Pressed Apple Juice",
            pack_count=1,
            unit_size_ml=1000,
            package_type="bottle",
            search_term="apple juice",
        )
        self.mapping = AldiMapping(
            catalog_id=self.pack.catalog_id,
            expected_product_name="Pure Pressed Apple Juice",
            source_product_id="000000000000399029",
        )

    def test_mapped_pack_is_observed_through_the_registered_client(self):
        page = {
            "meta": {"pagination": {"offset": 0, "limit": 6, "totalCount": 1}},
            "data": [_priced_apple_juice()],
        }
        client = AldiClient(
            opener=_RecordingOpener([json.dumps(page)]), min_request_interval=0
        )

        with tempfile.TemporaryDirectory() as directory:
            database = Path(directory) / "feed.sqlite"
            summary = collect_aldi_one(self.pack, self.mapping, client, database)

            self.assertEqual(summary["status"], "observed")
            self.assertEqual(summary["observed_count"], 1)
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
            ("aldi", "000000000000399029", "1.49", None, None,
             "EUR", "1.49", "1.4900"),
        )
        self.assertEqual(result, ("observed",))


if __name__ == "__main__":
    unittest.main()
