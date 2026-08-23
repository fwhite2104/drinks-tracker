"""Hermetic tests for the Operator Dashboard HTTP app and launcher."""

from __future__ import annotations

import json
import socket
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from http.client import HTTPResponse
from pathlib import Path

from beverage_feed.collector import AldiMapping, BenchmarkPack, collect_aldi_one
from beverage_feed.dashboard import (
    DEFAULT_HOST,
    DashboardApp,
    create_server,
    handle_request,
    main as dashboard_main,
)


PACK = BenchmarkPack(
    catalog_id="water-5l",
    name="Comeragh Still Water 5L Bottle",
    brand="Comeragh",
    variant="Still Water",
    pack_count=1,
    unit_size_ml=5000,
    package_type="bottle",
    search_term="Still Water",
)


def _write_json(path: Path, value: object) -> None:
    path.write_text(json.dumps(value, indent=2) + "\n")


def _minimal_workspace(root: Path) -> Path:
    data = root / "data"
    data.mkdir(parents=True, exist_ok=True)
    _write_json(
        data / "catalog.json",
        [
            {
                "catalog_id": PACK.catalog_id,
                "name": PACK.name,
                "brand": PACK.brand,
                "variant": PACK.variant,
                "pack_count": PACK.pack_count,
                "unit_size_ml": PACK.unit_size_ml,
                "package_type": PACK.package_type,
                "search_term": PACK.search_term,
            }
        ],
    )
    _write_json(
        data / "mappings.json",
        {
            "aldi": [
                {
                    "catalog_id": PACK.catalog_id,
                    "expected_product_name": "Still Water",
                    "status": "approved",
                }
            ]
        },
    )
    _write_json(data / "rejections.json", {"listings": [], "cells": []})
    return root


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind((DEFAULT_HOST, 0))
        return int(sock.getsockname()[1])


def _http_get(url: str) -> tuple[int, str, dict[str, str]]:
    request = urllib.request.Request(url, method="GET")
    try:
        with urllib.request.urlopen(request, timeout=5) as response:
            assert isinstance(response, HTTPResponse)
            body = response.read().decode("utf-8")
            headers = {k.lower(): v for k, v in response.headers.items()}
            return response.status, body, headers
    except urllib.error.HTTPError as exc:
        body = exc.read().decode("utf-8")
        headers = {k.lower(): v for k, v in exc.headers.items()} if exc.headers else {}
        return exc.code, body, headers


class HandleRequestTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.root = _minimal_workspace(Path(self._tmp.name))
        self.app = DashboardApp(self.root)

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def test_index_renders_operator_shell(self) -> None:
        status, body, content_type = handle_request(self.app, "GET", "/", {})
        text = body.decode("utf-8")
        self.assertEqual(status, 200)
        self.assertIn("text/html", content_type)
        self.assertIn("Overview", text)
        self.assertIn("Benchmark Catalog", text)
        self.assertIn("Consumer feed", text)
        self.assertIn("Feed not initialized", text)
        self.assertIn("Read-only mode", text)

    def test_overview_api_truthful_empty_state(self) -> None:
        status, body, _ = handle_request(self.app, "GET", "/api/overview", {})
        payload = json.loads(body.decode("utf-8"))
        self.assertEqual(status, 200)
        self.assertEqual(payload["stats"]["workspace_state"], "no_database")
        self.assertEqual(payload["stats"]["observation_count"], 0)
        self.assertEqual(payload["stats"]["catalog_packs"], 1)
        self.assertTrue(
            all(row["state"] == "not_collected" for row in payload["collection_health"])
        )

    def test_feed_api_consumer_labels(self) -> None:
        status, body, _ = handle_request(self.app, "GET", "/api/feed", {})
        payload = json.loads(body.decode("utf-8"))
        self.assertEqual(status, 200)
        self.assertIn("stock", payload["standing_rule"].lower())
        pack = payload["packs"][0]
        cells = {cell["retailer"]: cell for cell in pack["retailers"]}
        self.assertEqual(cells["aldi"]["label"], "Awaiting price")
        self.assertEqual(cells["tesco"]["label"], "Not available")

    def test_pack_api_404_for_unknown(self) -> None:
        status, body, _ = handle_request(
            self.app, "GET", "/api/pack/does-not-exist", {}
        )
        self.assertEqual(status, 404)
        self.assertIn("not found", json.loads(body.decode("utf-8"))["error"])

    def test_pack_api_returns_detail(self) -> None:
        status, body, _ = handle_request(
            self.app, "GET", f"/api/pack/{PACK.catalog_id}", {}
        )
        payload = json.loads(body.decode("utf-8"))
        self.assertEqual(status, 200)
        self.assertEqual(payload["catalog_id"], PACK.catalog_id)
        aldi = next(r for r in payload["retailers"] if r["retailer"] == "aldi")
        self.assertEqual(aldi["mapping_state"], "approved")

    def test_post_rejected(self) -> None:
        status, _, _ = handle_request(self.app, "POST", "/api/overview", {})
        self.assertEqual(status, 405)

    def test_coverage_and_discovery_endpoints(self) -> None:
        for path in (
            "/api/coverage",
            "/api/discovery",
            "/api/collection",
            "/api/catalog",
            "/api/retailers",
            "/api/workspace",
        ):
            status, body, _ = handle_request(self.app, "GET", path, {})
            self.assertEqual(status, 200, path)
            json.loads(body.decode("utf-8"))


class LiveServerTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.root = _minimal_workspace(Path(self._tmp.name))
        database = self.root / "data" / "feed.sqlite"
        collect_aldi_one(
            PACK,
            AldiMapping(catalog_id=PACK.catalog_id, expected_product_name="Still Water"),
            lambda _: {
                "items": [
                    {
                        "productId": "000000000000336021",
                        "name": "Still Water",
                        "brand": "COMERAGH",
                        "price": "€1.45",
                    }
                ]
            },
            database,
        )
        self.port = _free_port()
        self.app = DashboardApp(self.root, database_path=database)
        self.server = create_server(self.app, host=DEFAULT_HOST, port=self.port)
        self.thread = threading.Thread(
            target=self.server.serve_forever, kwargs={"poll_interval": 0.1}, daemon=True
        )
        self.thread.start()
        self.base = f"http://{DEFAULT_HOST}:{self.port}"

    def tearDown(self) -> None:
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=5)
        self._tmp.cleanup()

    def test_server_serves_index_and_feed_with_price(self) -> None:
        status, body, headers = _http_get(self.base + "/")
        self.assertEqual(status, 200)
        self.assertIn("text/html", headers.get("content-type", ""))
        self.assertIn("pourpoint", body.lower())

        status, body, _ = _http_get(self.base + "/api/feed")
        self.assertEqual(status, 200)
        payload = json.loads(body)
        aldi = next(
            c
            for c in payload["packs"][0]["retailers"]
            if c["retailer"] == "aldi"
        )
        self.assertEqual(aldi["state"], "observed")
        self.assertEqual(aldi["displayed_price"], "1.45")

    def test_server_overview_partial_run(self) -> None:
        status, body, _ = _http_get(self.base + "/api/overview")
        self.assertEqual(status, 200)
        payload = json.loads(body)
        self.assertEqual(payload["stats"]["workspace_state"], "partial_run")
        self.assertEqual(payload["stats"]["observation_count"], 1)


class LauncherMainTests(unittest.TestCase):
    def test_missing_catalog_is_fatal(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "data").mkdir()
            code = dashboard_main(
                [
                    "--repo-root",
                    str(root),
                    "--catalog",
                    str(root / "data" / "missing.json"),
                    "--no-browser",
                    "--port",
                    str(_free_port()),
                ]
            )
            self.assertEqual(code, 2)

    def test_refuses_non_loopback_host(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = _minimal_workspace(Path(tmp))
            code = dashboard_main(
                [
                    "--repo-root",
                    str(root),
                    "--host",
                    "0.0.0.0",
                    "--no-browser",
                    "--port",
                    str(_free_port()),
                ]
            )
            self.assertEqual(code, 2)

    def test_missing_repo_root_is_fatal(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            # Empty dir with no catalog upward — pass explicit missing root
            # that has no data/catalog.json.
            root = Path(tmp) / "empty"
            root.mkdir()
            code = dashboard_main(
                [
                    "--repo-root",
                    str(root),
                    "--no-browser",
                    "--port",
                    str(_free_port()),
                ]
            )
            self.assertEqual(code, 2)


if __name__ == "__main__":
    unittest.main()
