"""Hermetic tests for the review-sprint dashboard prototype (ticket 05).

The sprint delegates every decision to the real discovery_cli /
discovery_decisions seam, so these tests verify the HTTP wiring and the
durable writes landing in a temp DiscoveryStore — no live retailers, and the
repository's own data/ files are never touched.
"""

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

from beverage_feed.dashboard import DEFAULT_HOST
from beverage_feed.dashboard_sprint import (
    DEFAULT_PORT,
    SprintApp,
    create_server,
    handle_request,
    main as sprint_main,
)
from beverage_feed.discovery import DiscoveryStore

PACKS = [
    {
        "catalog_id": "coca-diet-330",
        "name": "Diet Coke 330ml Can",
        "brand": "Coca-Cola",
        "variant": "Diet",
        "pack_count": 1,
        "unit_size_ml": 330,
        "package_type": "can",
        "search_term": "Diet Coke",
        "aliases": ["Diet Coke"],
    },
    {
        "catalog_id": "coca-original-330",
        "name": "Coca-Cola Original Taste 330ml Can",
        "brand": "Coca-Cola",
        "variant": "Original Taste",
        "pack_count": 1,
        "unit_size_ml": 330,
        "package_type": "can",
        "search_term": "Coca-Cola Original",
    },
]


def _dunnes_record(name: str, reference: str = "111") -> dict[str, object]:
    return {
        "productName": name,
        "productReference": reference,
        "itemId": "222",
        "price": 2.5,
    }


def _write_json(path: Path, value: object) -> None:
    path.write_text(json.dumps(value, indent=2) + "\n")


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind((DEFAULT_HOST, 0))
        return int(sock.getsockname()[1])


def _http_request(
    url: str, *, method: str = "GET", payload: dict | None = None
) -> tuple[int, str]:
    data = json.dumps(payload).encode() if payload is not None else None
    request = urllib.request.Request(
        url, data=data, method=method,
        headers={"Content-Type": "application/json"} if data else {},
    )
    try:
        with urllib.request.urlopen(request, timeout=5) as response:
            assert isinstance(response, HTTPResponse)
            return response.status, response.read().decode("utf-8")
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read().decode("utf-8")


class SprintWorkspaceTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name)
        (self.root / "data").mkdir(parents=True)
        _write_json(self.root / "data" / "catalog.json", PACKS)
        _write_json(self.root / "data" / "mappings.json", {})
        _write_json(self.root / "data" / "rejections.json", {"listings": [], "cells": []})
        self.database = self.root / "data" / "discovery.sqlite"
        self.store = DiscoveryStore(self.database)
        self._seed()

    def _seed(self) -> None:
        """Class-A candidate (clean, name-derived) plus a review cell."""
        self.store.upsert_candidate(
            "dunnes:111:222",
            retailer="dunnes",
            identity_key="dunnes:111:222",
            identity_basis="composite",
            identity_tier="product",
            source_product_name="Diet Coke 330ml Can",
            source_product_reference="111",
            source_item_id="222",
            raw_record=_dunnes_record("Diet Coke 330ml Can"),
        )
        self.store.associate_candidate(
            "dunnes:111:222", "coca-diet-330", "Diet Coke", retailer="dunnes"
        )
        self.store.record_evidence(
            "dunnes:111:222",
            "coca-diet-330",
            retailer="dunnes",
            raw_price_value="2.50",
            price_parse_status="valid",
        )
        self.store.upsert_candidate(
            "dunnes:333:444",
            retailer="dunnes",
            identity_key="dunnes:333:444",
            identity_basis="composite",
            identity_tier="product",
            source_product_name="Coke Zero 330ml Can",
            source_product_reference="333",
            source_item_id="444",
        )
        self.store.associate_candidate(
            "dunnes:333:444", "coca-original-330", "Coca-Cola", retailer="dunnes"
        )
        self.store.record_evidence(
            "dunnes:333:444",
            "coca-original-330",
            retailer="dunnes",
            normalized_attributes={"brand": "Coca-Cola", "variant": "Zero"},
            raw_price_value=None,
            price_parse_status="missing",
        )
        self.store.set_cell_state(
            "dunnes", "coca-original-330", "review",
            review_category="missing", candidate_id="dunnes:333:444",
            decided_by="discovery", reason="no exact-pack candidate",
        )
        self.app = SprintApp(
            self.root,
            database_path=self.database,
            decided_by="sprint-tester",
        )

    def _decide(self, **payload: object) -> tuple[int, str]:
        body = json.dumps(payload).encode()
        status, out, _ = handle_request(self.app, "POST", "/api/sprint/decide", {}, body)
        return status, out.decode("utf-8")

    # -- shell & reads ------------------------------------------------------

    def test_index_renders_sprint_shell(self) -> None:
        status, body, content_type = handle_request(self.app, "GET", "/", {})
        text = body.decode("utf-8")
        self.assertEqual(status, 200)
        self.assertIn("text/html", content_type)
        self.assertIn("Review sprint", text)
        self.assertIn("Side-by-side", text)
        self.assertIn("Audit trail", text)
        self.assertIn("sprint-tester", text)

    def test_queue_serves_classified_items_with_comparison(self) -> None:
        status, body, _ = handle_request(self.app, "GET", "/api/sprint/queue", {})
        payload = json.loads(body.decode("utf-8"))
        self.assertEqual(status, 200)
        self.assertEqual(payload["counts"]["A"], 1)
        items = {(i["retailer"], i["catalog_id"]): i for i in payload["items"]}
        clean = items[("dunnes", "coca-diet-330")]
        self.assertEqual(clean["class"], "A")
        self.assertEqual(clean["candidate_id"], "dunnes:111:222")
        self.assertEqual(clean["evidence"]["price"], "2.50")
        # Brand Alias: "Diet Coke" matches the catalog brand via alias.
        brand_row = next(r for r in clean["comparison"] if r["key"] == "brand")
        self.assertTrue(brand_row["match"])
        review = items[("dunnes", "coca-original-330")]
        self.assertEqual(review["review_category"], "missing")
        self.assertEqual(review["evidence"]["price_status"], "missing")

    def test_progress_buckets_against_the_cell_bar(self) -> None:
        status, body, _ = handle_request(self.app, "GET", "/api/sprint/progress", {})
        payload = json.loads(body.decode("utf-8"))
        self.assertEqual(status, 200)
        self.assertEqual(payload["total_cells"], 10)
        self.assertEqual(sum(payload["buckets"].values()), 10)
        self.assertEqual(payload["buckets"]["in_review"], 1)
        self.assertEqual(payload["buckets"]["untouched"], 9)

    def test_audit_lists_transitions_after_a_decision(self) -> None:
        self._decide(
            action="approve", retailer="dunnes", catalog_id="coca-diet-330",
            candidate_id="dunnes:111:222", reason="clean class A",
        )
        status, body, _ = handle_request(self.app, "GET", "/api/sprint/audit", {})
        payload = json.loads(body.decode("utf-8"))
        self.assertEqual(status, 200)
        transition = payload["transitions"][0]
        self.assertEqual(transition["to_state"], "approved")
        self.assertEqual(transition["changed_by"], "sprint-tester")

    # -- decisions through the real seam ------------------------------------

    def test_approve_writes_mapping_json_and_cell_state(self) -> None:
        status, body = self._decide(
            action="approve", retailer="dunnes", catalog_id="coca-diet-330",
            candidate_id="dunnes:111:222", reason="clean class A",
        )
        self.assertEqual(status, 200)
        mappings = json.loads((self.root / "data" / "mappings.json").read_text())
        row = mappings["dunnes"][0]
        self.assertEqual(row["status"], "approved")
        self.assertEqual(row["decision_kind"], "operator")
        self.assertEqual(row["decided_by"], "sprint-tester")
        self.assertEqual(row["candidate_id"], "dunnes:111:222")
        with self.app.store().connection() as connection:
            state = connection.execute(
                "SELECT state FROM discovery_cells "
                "WHERE retailer='dunnes' AND catalog_id='coca-diet-330'"
            ).fetchone()[0]
        self.assertEqual(state, "approved")
        self.assertIn("approved", body)

    def test_reject_writes_rejections_json(self) -> None:
        status, _ = self._decide(
            action="reject", retailer="dunnes", catalog_id="coca-original-330",
            candidate_id="dunnes:333:444", reason="wrong variant",
        )
        self.assertEqual(status, 200)
        rejections = json.loads((self.root / "data" / "rejections.json").read_text())
        record = rejections["listings"][0]
        self.assertEqual(record["state"], "rejected")
        self.assertEqual(record["decided_by"], "sprint-tester")
        self.assertEqual(record["reason"], "wrong variant")

    def test_exclude_writes_do_not_map_cell(self) -> None:
        status, _ = self._decide(
            action="exclude", retailer="dunnes", catalog_id="coca-original-330",
            reason="retailer does not stock this pack",
        )
        self.assertEqual(status, 200)
        rejections = json.loads((self.root / "data" / "rejections.json").read_text())
        record = rejections["cells"][0]
        self.assertEqual(record["state"], "do_not_map")
        self.assertEqual(record["retailer"], "dunnes")

    def test_challenge_keep_resolves_through_the_decision_core(self) -> None:
        _write_json(
            self.root / "data" / "mappings.json",
            {
                "dunnes": [
                    {
                        "catalog_id": "coca-diet-330",
                        "expected_product_name": "Diet Coke 330ml Can",
                        "status": "approved",
                        "candidate_id": "dunnes:111:222",
                        "identity_tier": "product",
                    }
                ]
            },
        )
        self.store.set_cell_state(
            "dunnes", "coca-diet-330", "review",
            review_category="challenge", candidate_id="dunnes:333:444",
            decided_by="discovery", reason="late challenger",
        )
        status, _ = self._decide(
            action="challenge", challenge_action="keep",
            retailer="dunnes", catalog_id="coca-diet-330",
        )
        self.assertEqual(status, 200)
        with self.app.store().connection() as connection:
            state = connection.execute(
                "SELECT state FROM discovery_cells "
                "WHERE retailer='dunnes' AND catalog_id='coca-diet-330'"
            ).fetchone()[0]
        self.assertEqual(state, "approved")

    def test_decision_errors_return_400(self) -> None:
        status, body = self._decide(
            action="approve", retailer="dunnes", catalog_id="coca-diet-330",
            candidate_id="dunnes:does-not-exist",
        )
        self.assertEqual(status, 400)
        self.assertIn("unknown", body)

    def test_decide_rejects_get_and_bad_retailer(self) -> None:
        status, _, _ = handle_request(self.app, "GET", "/api/sprint/decide", {})
        self.assertEqual(status, 405)
        status, body = self._decide(
            action="exclude", retailer="marks", catalog_id="coca-diet-330"
        )
        self.assertEqual(status, 400)
        self.assertIn("unsupported retailer", body)

    # -- batch --------------------------------------------------------------

    def test_batch_applies_each_item_through_the_same_seam(self) -> None:
        payload = json.dumps({
            "action": "approve",
            "items": [
                {"retailer": "dunnes", "catalog_id": "coca-diet-330",
                 "candidate_id": "dunnes:111:222"},
                {"retailer": "aliens", "catalog_id": "coca-diet-330",
                 "candidate_id": "dunnes:111:222"},
            ],
        }).encode()
        status, body, _ = handle_request(self.app, "POST", "/api/sprint/batch", {}, payload)
        result = json.loads(body.decode("utf-8"))
        self.assertEqual(status, 200)
        self.assertEqual(result["applied"], 1)
        self.assertEqual(result["skipped"], 1)
        mappings = json.loads((self.root / "data" / "mappings.json").read_text())
        self.assertEqual(mappings["dunnes"][0]["status"], "approved")
        # Batch action itself is on the audit trail.
        status, body, _ = handle_request(self.app, "GET", "/api/sprint/audit", {})
        events = [row["event"] for row in json.loads(body.decode("utf-8"))["diagnostics"]]
        self.assertIn("sprint_batch", events)

    def test_batch_requires_items_and_known_action(self) -> None:
        for payload in (
            {"action": "approve", "items": []},
            {"action": "teleport", "items": [{"retailer": "dunnes"}]},
        ):
            status, _, _ = handle_request(
                self.app, "POST", "/api/sprint/batch", {},
                json.dumps(payload).encode(),
            )
            self.assertEqual(status, 400)

    # -- launcher -----------------------------------------------------------

    def test_main_refuses_missing_database(self) -> None:
        exit_code = sprint_main([
            "--repo-root", str(self.root),
            "--database", str(self.root / "data" / "absent.sqlite"),
            "--no-browser",
        ])
        self.assertEqual(exit_code, 2)

    def test_live_server_round_trip(self) -> None:
        port = _free_port()
        server = create_server(self.app, host=DEFAULT_HOST, port=port)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            base = f"http://{DEFAULT_HOST}:{port}"
            status, text = _http_request(f"{base}/")
            self.assertEqual(status, 200)
            self.assertIn("Review sprint", text)
            status, text = _http_request(f"{base}/api/sprint/queue")
            self.assertEqual(status, 200)
            self.assertIn("coca-diet-330", text)
            status, _ = _http_request(
                f"{base}/api/sprint/decide", method="POST",
                payload={
                    "action": "exclude", "retailer": "dunnes",
                    "catalog_id": "coca-original-330", "reason": "prototype round trip",
                },
            )
            self.assertEqual(status, 200)
            rejections = json.loads(
                (self.root / "data" / "rejections.json").read_text()
            )
            self.assertEqual(rejections["cells"][0]["state"], "do_not_map")
        finally:
            server.shutdown()
            server.server_close()


if __name__ == "__main__":
    unittest.main()
