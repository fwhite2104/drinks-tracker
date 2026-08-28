"""Tests for the Lidl IE collection client (full-feed-coverage #09).

Fixtures are trimmed recordings of the real API shape captured during research
ticket 02 (``.scratch/full-feed-coverage/research/lidl/``): the
``/q/api/search`` drinks-category response and the
``/p/api/detail/{productId}/IE/en`` detail response.  Tests never call live
Lidl endpoints.
"""

from __future__ import annotations

import io
import json
import sqlite3
import tempfile
import urllib.parse
from contextlib import closing
from pathlib import Path
import unittest

from beverage_feed.collector import BenchmarkPack, LidlMapping, collect_lidl_one
from beverage_feed.lidl import (
    LIDL_API_VERSION,
    LIDL_ASSORTMENT,
    LIDL_FETCH_SIZE,
    LIDL_LOCALE,
    LIDL_SEARCH_ENDPOINT,
    LidlClient,
    parse_title_pack,
)


def _tymbark_search_item() -> dict:
    """Trimmed recording of one drinks-category gridbox (research ticket 02)."""
    return {
        "resultClass": "product",
        "type": "product",
        "code": "11214651",
        "gridbox": {
            "data": {
                "canonicalPath": "/p/tymbark-apple-cherry-juice/p11214651",
                "erpNumber": "11214651",
                "fullTitle": "- TYMBARK Apple Cherry Juice",
                "keyfacts": {
                    "fullTitle": "- TYMBARK Apple Cherry Juice",
                    "title": "TYMBARK Apple Cherry Juice",
                    "wonCategoryPrimary": (
                        "Worlds of need/Food and near food/Drinks/Fruit juices"
                    ),
                },
                "multipack": False,
                "price": {"currencyCode": "EUR", "price": 1.39, "specialTaxes": []},
                "productId": 11214651,
                "title": "TYMBARK Apple Cherry Juice ",
            },
            "meta": {
                "ean": "5900334000781",
                "fullTitle": "- TYMBARK Apple Cherry Juice",
            },
        },
    }


def _hata_cola_search_item() -> dict:
    """Trimmed recording of the HATA Cola gridbox; deposit lives in basePrice."""
    return {
        "resultClass": "product",
        "type": "product",
        "code": "11258557",
        "gridbox": {
            "data": {
                "canonicalPath": "/p/hata-cola-drink/p11258557",
                "erpNumber": "11258557",
                "fullTitle": "HATA Cola Drink",
                "keyfacts": {
                    "fullTitle": "HATA Cola Drink",
                    "title": "HATA Cola Drink",
                    "wonCategoryPrimary": (
                        "Worlds of need/Food and near food/Drinks/Soft drinks"
                    ),
                },
                "multipack": False,
                "price": {
                    "basePrice": {"prefix": False, "text": "€0.15 Deposit Return"},
                    "currencyCode": "EUR",
                    "price": 1.89,
                    "specialTaxes": [],
                },
                "productId": 11258557,
                "title": "HATA Cola Drink",
            },
            "meta": {"ean": "4902494170022", "fullTitle": "HATA Cola Drink"},
        },
    }


def _drinks_search_page() -> dict:
    """Trimmed recording of GET /q/api/search?category.id=10071022&..."""
    return {
        "resultType": "products",
        "numFound": 8,
        "offset": 0,
        "fetchsize": LIDL_FETCH_SIZE,
        "items": [_tymbark_search_item(), _hata_cola_search_item()],
    }


def _tymbark_detail() -> dict:
    """Trimmed recording of GET /p/api/detail/11214651/IE/en."""
    return {
        "canonicalPath": "/p/tymbark-apple-cherry-juice/p11214651",
        "eans": ["5900334000781"],
        "erpNumber": "11214651",
        "havingPrice": True,
        "keyfacts": {
            "fullTitle": "- TYMBARK Apple Cherry Juice",
            "title": "TYMBARK Apple Cherry Juice",
        },
        "multipack": False,
        "price": {"currencyCode": "EUR", "price": 1.39, "specialTaxes": []},
        "productId": 11214651,
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


class TitlePackParsingTests(unittest.TestCase):
    """Titles are the only in-source pack-size signal (confirmed: neither the
    listing payload nor the detail API carries structured pack size), so the
    parser must be conservative — no size in the title, no invented pack."""

    def test_explicit_multipack_titles_give_pack_count(self):
        self.assertEqual(parse_title_pack("Coca-Cola 6 x 330ml Cans"), (6, 330))
        self.assertEqual(parse_title_pack("7UP 4X1.5L Bottles"), (4, 1500))

    def test_trailing_multipack_count_is_honoured(self):
        self.assertEqual(parse_title_pack("Coca-Cola 330ml Cans x6"), (6, 330))
        self.assertEqual(parse_title_pack("Robinsons 1.5L x12"), (12, 1500))

    def test_single_unit_titles_give_unit_size(self):
        self.assertEqual(parse_title_pack("Coca-Cola Zero Sugar 330ml Can"), (1, 330))
        self.assertEqual(parse_title_pack("Coca-Cola 1.5L Bottle"), (1, 1500))
        self.assertEqual(parse_title_pack("HATA Cola Drink 500ML"), (1, 500))

    def test_titles_without_size_return_none(self):
        # Live Lidl IE drink titles mostly carry no size ("HATA Cola Drink").
        self.assertIsNone(parse_title_pack("HATA Cola Drink"))
        self.assertIsNone(parse_title_pack("TYMBARK Apple Cherry Juice"))
        self.assertIsNone(parse_title_pack("Pink Guava Nectar"))
        self.assertIsNone(parse_title_pack(""))
        self.assertIsNone(parse_title_pack(None))

    def test_non_volume_fractions_do_not_invent_sizes(self):
        self.assertIsNone(parse_title_pack("Robinsons 6 Pack"))
        self.assertIsNone(parse_title_pack("Lidl 2 for 1 Offer"))


class LidlClientSearchTests(unittest.TestCase):
    def test_searches_and_normalizes_gridbox_records(self):
        opener = _RecordingOpener([json.dumps(_drinks_search_page())])
        client = LidlClient(opener=opener, min_request_interval=0)

        payload = client("cola")

        self.assertEqual(payload["pagination"], {"total": 8, "offset": 0})
        first = payload["items"][0]
        self.assertEqual(first["productId"], "11214651")
        self.assertEqual(first["name"], "TYMBARK Apple Cherry Juice")
        self.assertEqual(first["price"], "€1.39")
        self.assertEqual(first["multipack"], False)
        self.assertEqual(first["gtin"], "5900334000781")
        # The live title carries no pack size: no pack evidence is invented.
        self.assertNotIn("packCount", first)
        self.assertNotIn("unitSizeMl", first)
        self.assertEqual(payload["items"][1]["gtin"], "4902494170022")

        url = opener.requests[0].full_url
        self.assertIn(f"{LIDL_SEARCH_ENDPOINT}?", url)
        self.assertIn("q=cola", url)
        self.assertIn(f"locale={LIDL_LOCALE}", url)
        self.assertIn(f"assortment={LIDL_ASSORTMENT}", url)
        self.assertIn(f"version={LIDL_API_VERSION}", url)
        self.assertIn(f"fetchsize={LIDL_FETCH_SIZE}", url)
        self.assertEqual(
            opener.requests[0].get_header("User-agent"), "drinks-tracker/0.1"
        )

    def test_title_pack_size_becomes_pack_evidence(self):
        page = _drinks_search_page()
        page["items"][0]["gridbox"]["data"]["keyfacts"][
            "title"
        ] = "TYMBARK Apple Cherry Juice 6 x 1L"
        opener = _RecordingOpener([json.dumps(page)])
        client = LidlClient(opener=opener, min_request_interval=0)

        record = client("apple cherry juice")["items"][0]

        self.assertEqual(record["packCount"], 6)
        self.assertEqual(record["unitSizeMl"], 1000)

    def test_empty_result_type_is_a_complete_empty_page(self):
        opener = _RecordingOpener([json.dumps({"resultType": "empty"})])
        client = LidlClient(opener=opener, min_request_interval=0)

        payload = client("chateau laffite")

        self.assertEqual(payload["items"], [])
        self.assertEqual(payload["pagination"]["total"], 0)

    def test_malformed_search_response_raises_source_error(self):
        client = LidlClient(
            opener=_RecordingOpener([json.dumps({"facets": []})]),
            min_request_interval=0,
        )
        with self.assertRaises(RuntimeError) as context:
            client("cola")
        self.assertIn("no items list", str(context.exception))

    def test_invalid_json_response_raises_source_error(self):
        client = LidlClient(
            opener=_RecordingOpener(["<html>Bad Request</html>"]),
            min_request_interval=0,
        )
        with self.assertRaises(RuntimeError) as context:
            client("cola")
        self.assertIn("not valid JSON", str(context.exception))

    def test_network_failure_raises_useful_error(self):
        class BrokenOpener:
            def open(self, request: urllib.request.Request, timeout: float) -> io.BytesIO:
                raise OSError("connection refused")

        client = LidlClient(opener=BrokenOpener(), min_request_interval=0)
        with self.assertRaises(RuntimeError) as context:
            client("cola")
        self.assertIn("Lidl request failed", str(context.exception))

    def test_empty_search_term_is_rejected(self):
        client = LidlClient(opener=_RecordingOpener([]), min_request_interval=0)
        with self.assertRaises(ValueError):
            client("   ")

    def test_negative_request_interval_is_rejected(self):
        with self.assertRaises(ValueError):
            LidlClient(min_request_interval=-1)


class LidlClientHydrationTests(unittest.TestCase):
    def test_fetches_priced_product_by_id_via_detail_api(self):
        opener = _RecordingOpener([json.dumps(_tymbark_detail())])
        client = LidlClient(opener=opener, min_request_interval=0)

        payload = client.fetch_product("11214651")

        record = payload["items"][0]
        self.assertEqual(record["productId"], "11214651")
        self.assertEqual(record["name"], "TYMBARK Apple Cherry Juice")
        self.assertEqual(record["price"], "€1.39")
        # The detail API exposes the EAN but still no structured pack size.
        self.assertEqual(record["gtin"], "5900334000781")
        self.assertNotIn("packCount", record)
        url = opener.requests[0].full_url
        self.assertIn(
            "https://www.lidl.ie/p/api/detail/11214651/IE/en", url
        )

    def test_unpriced_detail_payload_returns_empty_items(self):
        detail = _tymbark_detail()
        detail["havingPrice"] = False
        del detail["price"]
        client = LidlClient(
            opener=_RecordingOpener([json.dumps(detail)]), min_request_interval=0
        )

        self.assertEqual(client.fetch_product("11214651"), {"items": []})

    def test_empty_product_id_is_rejected(self):
        client = LidlClient(opener=_RecordingOpener([]), min_request_interval=0)
        with self.assertRaises(ValueError):
            client.fetch_product("  ")

    def test_detail_template_without_placeholder_is_rejected(self):
        with self.assertRaises(ValueError):
            LidlClient(detail_url_template="https://www.lidl.ie/p/api/detail")


class LidlCollectionIntegrationTests(unittest.TestCase):
    """The registered client satisfies the collect_lidl_one fetcher contract."""

    def setUp(self) -> None:
        self.pack = BenchmarkPack(
            catalog_id="tymbark-apple-cherry-juice-1l",
            name="TYMBARK Apple Cherry Juice 1L",
            brand="Tymbark",
            variant="Apple Cherry Juice",
            pack_count=1,
            unit_size_ml=1000,
            package_type="carton",
            search_term="apple cherry juice",
        )
        self.mapping = LidlMapping(
            catalog_id=self.pack.catalog_id,
            expected_product_name="TYMBARK Apple Cherry Juice",
            source_product_id="11214651",
        )

    def test_mapped_pack_is_observed_through_the_registered_client(self):
        client = LidlClient(
            opener=_RecordingOpener([json.dumps(_tymbark_detail())]),
            min_request_interval=0,
        )

        with tempfile.TemporaryDirectory() as directory:
            database = Path(directory) / "feed.sqlite"
            summary = collect_lidl_one(self.pack, self.mapping, client, database)

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
            ("lidl", "11214651", "1.39", None, None, "EUR", "1.39", "1.3900"),
        )
        self.assertEqual(result, ("observed",))

    def test_deposit_in_base_price_text_is_recorded_separately(self):
        pack = BenchmarkPack(
            catalog_id="hata-cola-drink-330",
            name="HATA Cola Drink 330ml",
            brand="HATA",
            variant="Cola Drink",
            pack_count=1,
            unit_size_ml=330,
            package_type="bottle",
            search_term="hata cola",
        )
        mapping = LidlMapping(
            catalog_id=pack.catalog_id,
            expected_product_name="HATA Cola Drink",
        )
        page = _drinks_search_page()
        client = LidlClient(
            opener=_RecordingOpener([json.dumps(page)]), min_request_interval=0
        )

        with tempfile.TemporaryDirectory() as directory:
            database = Path(directory) / "feed.sqlite"
            summary = collect_lidl_one(pack, mapping, client, database)

            self.assertEqual(summary["status"], "observed")
            with closing(sqlite3.connect(database)) as connection:
                observation = connection.execute(
                    """
                    SELECT displayed_price, drs_deposit, source_product_reference
                    FROM price_observations
                    """
                ).fetchone()

        self.assertEqual(observation, ("1.89", "0.15", "11258557"))


if __name__ == "__main__":
    unittest.main()
