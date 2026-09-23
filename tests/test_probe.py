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

SYNTHETIC_MAPPINGS = {
    "tesco": [
        {"catalog_id": "coca-diet-2000", "expected_product_name": "Diet Coke 2 Litre", "source_tpnb": "92752847", "status": "approved"}
    ]
}


class StubTransport:
    """Replays canned payloads and records the requests it was given."""

    def __init__(
        self,
        *,
        json_by_url: dict[str, Any] | None = None,
        html_by_url: dict[str, str] | None = None,
        fail_urls: set[str] | None = None,
        error_records_by_url: dict[str, dict[str, Any]] | None = None,
    ) -> None:
        self.json_by_url = json_by_url or {}
        self.html_by_url = html_by_url or {}
        self.fail_urls = fail_urls or set()
        self.error_records_by_url = error_records_by_url or {}
        self.requests: list[urllib.request.Request] = []
        self.bodies: list[Any] = []
        self.last_body: Any = None

    def send(
        self,
        request: urllib.request.Request,
        *,
        parse_json: bool = True,
        capture_errors: bool = False,
    ) -> Any:
        self.requests.append(request)
        url = request.full_url
        data = request.data
        if isinstance(data, (bytes, bytearray)):
            body = json.loads(bytes(data).decode())
            self.bodies.append(body)
            self.last_body = body
        for failed in self.fail_urls:
            if url.startswith(failed):
                raise RuntimeError(f"HTTP 403 for {failed}")
        if url in self.error_records_by_url and capture_errors:
            return self.error_records_by_url[url]
        if parse_json:
            return self.json_by_url.get(url, {})
        return self.html_by_url.get(url, "<html></html>").encode()


@pytest.fixture()
def mappings_file(tmp_path: Path) -> Path:
    path = tmp_path / "mappings.json"
    path.write_text(json.dumps(SYNTHETIC_MAPPINGS))
    return path


def test_category_page_summary_extracts_links_blobs_and_values() -> None:
    summary = probe.summarize_category_page(SYNTHETIC_PAGE)

    assert summary["title"] == "Fizzy Drinks - Tesco Groceries"
    assert any("fizzy-drinks" in link for link in summary["drink_links"])
    assert all("fresh-food" not in link for link in summary["drink_links"])
    assert "dict" in {blob.get("shape") for blob in summary["json_blobs"]}
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


def test_batch_carries_the_control_operation_and_argless_elicitations(
    monkeypatch: pytest.MonkeyPatch, mappings_file: Path
) -> None:
    monkeypatch.setattr(probe, "MAPPING_PATH", mappings_file)
    transport = StubTransport(json_by_url={probe.TESCO_GRAPHQL_ENDPOINT: [SYNTHETIC_INTROSPECTION]})

    report = probe.probe_graphql(transport, "test-key")

    assert report["control_tpnb"] == "92752847"
    batch = transport.bodies[0]
    assert isinstance(batch, list)
    names = [operation["operationName"] for operation in batch]
    assert names[0] == "GetProductByTpnb"
    assert "ProbeBrowseCategory" in names
    assert batch[0]["variables"] == {"tpnb": "92752847"}
    # Every elicitation is argless: no invented argument values anywhere.
    for operation in batch[1:]:
        assert operation["variables"] == {}
        if not str(operation["operationName"]).startswith("SubfieldScan_"):
            assert "__typename" in operation["query"]


def test_batch_results_classify_answered_rejected_and_missing() -> None:
    operations = [
        {"operationName": "GetProductByTpnb"},
        {"operationName": "ProbeBrowseCategory"},
        {"operationName": "ProbeCategory"},
    ]
    payload = [
        {"data": {"product": {"tpnb": "1"}}},
        {"errors": [{"message": "Cannot query field 'browseCategory' on type 'Query'."}]},
    ]

    results = probe._summarize_batch(operations, payload)

    assert [row["outcome"] for row in results] == ["answered", "rejected", "missing reply"]
    assert "Cannot query field" in results[1]["errors"][0]
    assert results[0]["data_keys"] == ["product"]


def test_batch_results_handle_a_non_list_reply() -> None:
    results = probe._summarize_batch([{"operationName": "GetProductByTpnb"}], {"errors": []})

    assert results == [
        {
            "operation": "GetProductByTpnb",
            "outcome": "no batch reply",
            "reply_type": "dict",
        }
    ]


def test_http_error_responses_keep_the_retailer_explanation(
    monkeypatch: pytest.MonkeyPatch, mappings_file: Path
) -> None:
    monkeypatch.setattr(probe, "MAPPING_PATH", mappings_file)
    transport = StubTransport(
        error_records_by_url={
            probe.TESCO_GRAPHQL_ENDPOINT: {
                "error_response": True,
                "status": 400,
                "retry_after": None,
                "body": '{"errors":[{"message":"introspection is disabled"}]}',
                "json": {"errors": [{"message": "introspection is disabled"}]},
            }
        }
    )

    report = probe.probe_graphql(transport, "test-key")

    assert report["batch"]["outcome"] == "http_error"
    assert report["batch"]["http_status"] == 400
    assert "introspection is disabled" in report["batch"]["body"]
    assert report["introspection_summary"]["available"] is False


def test_run_writes_artifacts_and_uses_the_injected_transport(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, mappings_file: Path
) -> None:
    monkeypatch.setattr(probe, "MAPPING_PATH", mappings_file)
    transport = StubTransport(
        json_by_url={probe.TESCO_GRAPHQL_ENDPOINT: [SYNTHETIC_INTROSPECTION]},
        html_by_url={probe.DEFAULT_CATEGORY_URLS[0]: SYNTHETIC_PAGE},
    )

    summary = probe.run(
        transport,
        api_key="test-key",
        out_dir=tmp_path,
        category_urls=probe.DEFAULT_CATEGORY_URLS[:1],
    )

    assert summary["page_category_values"] == ["1234"]
    assert (tmp_path / "probe-summary.json").is_file()
    assert (tmp_path / "introspection-fields.json").is_file()
    assert (tmp_path / "category-page-1.json").is_file()
    # 1 page + shape batch chunks + argument batch chunks + 1 introspection
    # + category-id walk chunks (15 operations at TESCO_GRAPHQL_BATCH_LIMIT = 5)
    assert len(transport.requests) == 11


def test_arg_scan_splits_accepted_from_unknown() -> None:
    operations = [
        {"operationName": "ArgScan_category_id"},
        {"operationName": "ArgScan_category_path"},
        {"operationName": "ArgScan_productList_id"},
    ]
    payload = [
        {"errors": [{"message": 'Unknown argument "id" on field "Query.category".'}]},
        {"errors": [{"message": 'Argument "path" has invalid value "1". Expected type Int.'}]},
        {"data": {"productList": {"__typename": "GenericProductListType"}}},
    ]

    results = probe._summarize_batch(operations, payload)
    category = probe.classify_arg_scan(results, "category")
    product_list = probe.classify_arg_scan(results, "productList")

    assert [row["arg"] for row in category["accepted"]] == ["path"]
    assert "Expected type Int" in category["accepted"][0]["detail"]
    assert category["unknown"] == ["id"]
    assert [row["arg"] for row in product_list["accepted"]] == ["id"]
    assert product_list["unknown"] == []


def test_subfield_scan_infers_valid_fields_from_the_error_set() -> None:
    row = {
        "operation": "SubfieldScan_category",
        "outcome": "rejected",
        "errors": [
            'Cannot query field "title" on type "ProductListType".',
            'Cannot query field "nodes" on type "ProductListType".',
        ],
    }

    report = probe.classify_subfield_scan(row)

    assert report["available"] is True
    assert report["invalid"] == ["title", "nodes"]
    assert "products" in report["valid"]
    assert "pagination" in report["valid"]
    assert probe.classify_subfield_scan(None) == {"available": False}


def test_transport_failures_are_recorded_as_evidence(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, mappings_file: Path
) -> None:
    monkeypatch.setattr(probe, "MAPPING_PATH", mappings_file)
    transport = StubTransport(fail_urls={probe.TESCO_GRAPHQL_ENDPOINT})

    summary = probe.run(
        transport,
        api_key="test-key",
        out_dir=tmp_path,
        category_urls=probe.DEFAULT_CATEGORY_URLS[:1],
    )

    assert summary["graphql"]["batch"]["outcome"] == "error"
    assert "403" in summary["graphql"]["batch"]["error"]


def test_api_key_never_appears_in_the_artifact(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, mappings_file: Path
) -> None:
    monkeypatch.setattr(probe, "MAPPING_PATH", mappings_file)
    transport = StubTransport(
        json_by_url={probe.TESCO_GRAPHQL_ENDPOINT: [SYNTHETIC_INTROSPECTION]},
        html_by_url={probe.DEFAULT_CATEGORY_URLS[0]: SYNTHETIC_PAGE},
    )

    probe.run(
        transport,
        api_key="super-secret-key",
        out_dir=tmp_path,
        category_urls=probe.DEFAULT_CATEGORY_URLS[:1],
    )

    written = "\n".join(path.read_text() for path in sorted(tmp_path.glob("*.json")))
    assert "super-secret-key" not in written


def test_first_tesco_tpnb_reads_the_curated_mappings(mappings_file: Path, tmp_path: Path) -> None:
    assert probe._first_tesco_tpnb(mappings_file) == "92752847"
    assert probe._first_tesco_tpnb(tmp_path / "absent.json") is None
    empty = tmp_path / "empty.json"
    empty.write_text(json.dumps({"tesco": []}))
    assert probe._first_tesco_tpnb(empty) is None


def test_probe_records_the_aisle_url_without_query_values() -> None:
    assert probe._clean_url("https://www.tesco.ie/shop?token=abc&page=2") == (
        "https://www.tesco.ie/shop?token=…&page=…"
    )


def test_probe_main_requires_no_network(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, mappings_file: Path
) -> None:
    monkeypatch.setattr(probe, "MAPPING_PATH", mappings_file)
    transport = StubTransport(
        json_by_url={probe.TESCO_GRAPHQL_ENDPOINT: [SYNTHETIC_INTROSPECTION]},
        html_by_url={probe.DEFAULT_CATEGORY_URLS[0]: SYNTHETIC_PAGE},
    )
    monkeypatch.setattr(probe, "build_transport", lambda min_request_interval=2.5: transport)
    monkeypatch.setenv("TESCO_API_KEY", "test-key")

    exit_code = probe.main(["--out", str(tmp_path), "--category-url", probe.DEFAULT_CATEGORY_URLS[0]])

    assert exit_code == 0
    assert (tmp_path / "probe-summary.json").is_file()


SYNTHETIC_WALK_REPLY = [
    {"data": {"category": {"count": 0, "page": 1, "products": [],
                           "pagination": {"__typename": "Pagination"}}}},
    {"data": {"category": {"count": 0, "page": 1, "products": []}}},
    {"data": {"category": {"count": 214, "page": 1,
                           "products": [{"id": "92752847", "title": "Diet Coke 2L"},
                                        {"id": "12345678", "title": "Coca-Cola Zero 2L"}],
                           "pagination": {"__typename": "Pagination"}}}},
    {"data": {"category": {"count": 214, "page": 1,
                           "products": [{"id": "92752847", "gtin": "05000112633818",
                                         "title": "Diet Coke 2L"}]}}},
]


def test_category_id_sweep_reports_counts_and_negatives() -> None:
    """A zero-count id is a truthful negative; a counted id names the aisle."""
    operations = probe._category_id_operations("92752847", ("nope", "drinks"))
    assert [op["operationName"] for op in operations][:2] == [
        "DetailScan_categories", "DetailScan_categoryIds",
    ]
    assert "Walk_nope" in [op["operationName"] for op in operations]
    assert "WalkFull_drinks" in [op["operationName"] for op in operations]
    assert 'category(categoryId: "drinks")' in operations[-1]["query"]


def test_category_walk_summary_splits_answered_from_empty() -> None:
    """``tpnb=None`` keeps the operation list aligned with the reply below."""
    operations = probe._category_id_operations(None, ("nope", "drinks"))
    assert [op["operationName"] for op in operations] == [
        "Walk_nope", "WalkFull_nope", "Walk_drinks", "WalkFull_drinks",
    ]
    summaries = [
        {**entry, "data": reply.get("data")}
        for entry, reply in zip(probe._summarize_batch(operations, SYNTHETIC_WALK_REPLY),
                                SYNTHETIC_WALK_REPLY)
    ]
    rows = probe.classify_category_walk(summaries, ("nope", "drinks"))

    assert rows[0]["category_id"] == "nope"
    assert rows[0]["count"] == 0
    assert rows[0]["products_seen"] == 0
    assert rows[1]["category_id"] == "drinks"
    assert rows[1]["count"] == 214
    assert rows[1]["products_seen"] == 2
    assert rows[1]["full_outcome"] == "answered"


def test_category_walk_operations_are_graphql_name_safe() -> None:
    operations = probe._category_id_operations(None, ("fizzy-drinks",))
    assert operations[0]["operationName"] == "Walk_fizzy_drinks"
