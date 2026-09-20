"""Tests for the Tesco category-walk reconnaissance probe (ff-20).

The probe is CI-egress-only tooling; these tests never touch the network.
Every case injects a stub transport, and the fixtures are *synthetic* pages
written for these tests — not captured retailer payloads (the real capture is
the artifact the probe itself uploads).
"""

from __future__ import annotations

import json
import urllib.request
from pathlib import Path
from typing import Any

import pytest

from beverage_feed import probe

SYNTHETIC_PAGE = """<!DOCTYPE html>
<html><head><title>Fizzy Drinks - Tesco Groceries</title></head>
<body>
<a href="/groceries/en-IE/shop/drinks/fizzy-drinks/all">Fizzy Drinks</a>
<a href="/groceries/en-IE/shop/drinks/water/all">Water</a>
<a href="/groceries/en-IE/shop/fresh-food/all">Fresh Food</a>
<script type="application/json" id="__NEXT_DATA__">{"props":{"categoryId":"1234","products":[{"tpnb":"12345678","title":"Synthetic Cola 2L","price":2.5}]}}</script>
<script type="application/ld+json">{"@type":"BreadcrumbList"}</script>
</body></html>
"""

SYNTHETIC_INTROSPECTION = {
    "data": {
        "__schema": {
            "queryType": {
                "fields": [
                    {"name": "search", "args": [{"name": "query", "type": {"kind": "SCALAR", "name": "String"}}]},
                    {"name": "product", "args": [{"name": "tpnb", "type": {"kind": "SCALAR", "name": "String"}}]},
                    {
                        "name": "browseCategory",
                        "args": [
                            {
                                "name": "categoryId",
                                "type": {"kind": "NON_NULL", "name": None, "ofType": {"kind": "SCALAR", "name": "String"}},
                            },
                            {"name": "offset", "type": {"kind": "SCALAR", "name": "Int"}},
                        ],
                    },
                ]
            }
        }
    }
}


class StubTransport:
    """Records requests and replays canned payloads keyed by URL."""

    def __init__(self, *, json_by_url: dict[str, Any] | None = None, html_by_url: dict[str, str] | None = None,
                 fail_urls: set[str] | None = None) -> None:
        self.json_by_url = json_by_url or {}
        self.html_by_url = html_by_url or {}
        self.fail_urls = fail_urls or set()
        self.requests: list[urllib.request.Request] = []

    def send(self, request: urllib.request.Request, *, parse_json: bool = True) -> Any:
        self.requests.append(request)
        url = request.full_url
        for failed in self.fail_urls:
            if url.startswith(failed):
                raise RuntimeError(f"HTTP 403 for {failed}")
        if parse_json:
            return self.json_by_url.get(url, {})
        return self.html_by_url.get(url, "<html></html>").encode()


def test_category_page_summary_extracts_links_blobs_and_values() -> None:
    summary = probe.summarize_category_page(SYNTHETIC_PAGE)

    assert summary["title"] == "Fizzy Drinks - Tesco Groceries"
    assert any("fizzy-drinks" in link for link in summary["drink_links"])
    assert all("fresh-food" not in link for link in summary["drink_links"])
    shapes = {blob.get("shape") for blob in summary["json_blobs"]}
    assert "dict" in shapes
    assert "1234" in summary["category_values"]
    assert len(summary["sample"]) <= probe._HTML_SAMPLE_CHARS


def test_introspection_summary_surfaces_category_like_fields() -> None:
    summary = probe.summarize_introspection(SYNTHETIC_INTROSPECTION)

    assert summary["available"] is True
    assert summary["field_count"] == 3
    assert "browseCategory" in summary["category_like"]
    args = {arg["name"]: arg for arg in summary["category_like"]["browseCategory"]}
    assert args["categoryId"]["required"] is True
    assert args["offset"]["required"] is False
    assert args["categoryId"]["type"] == "String!"


def test_probe_pages_use_the_injected_transport() -> None:
    transport = StubTransport(
        json_by_url={probe.TESCO_GRAPHQL_ENDPOINT: SYNTHETIC_INTROSPECTION},
        html_by_url={probe.DEFAULT_CATEGORY_URLS[0]: SYNTHETIC_PAGE},
    )

    report = probe.probe_pages(transport, probe.DEFAULT_CATEGORY_URLS[:1])

    assert len(report) == 1
    rewritten = transport.requests[0]
    assert rewritten.get_method() == "GET"
    assert "Accept" in dict(rewritten.header_items())


def test_category_call_runs_with_a_discovered_category_value(tmp_path: Path) -> None:
    transport = StubTransport(
        json_by_url={probe.TESCO_GRAPHQL_ENDPOINT: SYNTHETIC_INTROSPECTION},
        html_by_url={probe.DEFAULT_CATEGORY_URLS[0]: SYNTHETIC_PAGE},
    )

    summary = probe.run(
        transport, api_key="test-key", out_dir=tmp_path, category_urls=probe.DEFAULT_CATEGORY_URLS[:1]
    )

    call = summary["graphql"]["category_call"]
    assert call["field"] == "browseCategory"
    assert call["attempt"]["outcome"] == "ok"
    assert "categoryId: $v0" in json.dumps(call["attempt"]) or "categoryId" in json.dumps(call["args"])
    assert (tmp_path / "probe-summary.json").is_file()
    assert (tmp_path / "introspection-fields.json").is_file()
    assert (tmp_path / "category-page-1.json").is_file()


def test_category_call_is_skipped_without_a_value_and_never_guesses() -> None:
    transport = StubTransport(json_by_url={probe.TESCO_GRAPHQL_ENDPOINT: SYNTHETIC_INTROSPECTION})

    report = probe.probe_graphql(transport, "test-key", category_values=())

    call = report["category_call"]
    assert call["outcome"] == "not attempted"
    assert any("required" in entry for entry in call["missing"])
    # Only the introspection request went out — no guessed call.
    assert len(transport.requests) == 1


def test_transport_failures_are_recorded_as_evidence(tmp_path: Path) -> None:
    transport = StubTransport(fail_urls={probe.TESCO_GRAPHQL_ENDPOINT})

    summary = probe.run(
        transport, api_key="test-key", out_dir=tmp_path, category_urls=probe.DEFAULT_CATEGORY_URLS[:1]
    )

    record = summary["graphql"]["introspection"]
    assert record["outcome"] == "error"
    assert "403" in record["error"]
    assert summary["graphql"]["introspection_summary"]["available"] is False


def test_api_key_never_appears_in_the_artifact(tmp_path: Path) -> None:
    transport = StubTransport(
        json_by_url={probe.TESCO_GRAPHQL_ENDPOINT: SYNTHETIC_INTROSPECTION},
        html_by_url={probe.DEFAULT_CATEGORY_URLS[0]: SYNTHETIC_PAGE},
    )

    probe.run(
        transport, api_key="super-secret-key", out_dir=tmp_path, category_urls=probe.DEFAULT_CATEGORY_URLS[:1]
    )

    written = "\n".join(
        path.read_text() for path in sorted(tmp_path.glob("*.json"))
    )
    assert "super-secret-key" not in written


def test_probe_records_the_aisle_url_without_query_values() -> None:
    assert probe._clean_url("https://www.tesco.ie/shop?token=abc&page=2") == (
        "https://www.tesco.ie/shop?token=…&page=…"
    )


def test_probe_main_requires_no_network(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    transport = StubTransport(
        json_by_url={probe.TESCO_GRAPHQL_ENDPOINT: SYNTHETIC_INTROSPECTION},
        html_by_url={probe.DEFAULT_CATEGORY_URLS[0]: SYNTHETIC_PAGE},
    )
    monkeypatch.setattr(probe, "build_transport", lambda min_request_interval=2.5: transport)
    monkeypatch.setenv("TESCO_API_KEY", "test-key")

    exit_code = probe.main(["--out", str(tmp_path), "--category-url", probe.DEFAULT_CATEGORY_URLS[0]])

    assert exit_code == 0
    assert (tmp_path / "probe-summary.json").is_file()
