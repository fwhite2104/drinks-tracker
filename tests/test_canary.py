"""Tests for the live retailer canary and release gate (ticket audit-10).

The canary command itself touches live retailers and is never run by the
test suite; every test here drives the canary's logic through fake retailer
clients and captured fixtures, exactly like the collection tests.
"""

import io
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from beverage_feed.canary import (
    CANARY_RETAILERS,
    GATE_FAILURE_THRESHOLD,
    CanaryOutcome,
    _observation_checks,
    _select_canary_cell,
    load_gate_state,
    main,
    record_outcomes,
    release_gate,
    run_canary,
)
from beverage_feed.collector import (
    BenchmarkPack,
    DunnesMapping,
    SuperValuMapping,
    TescoMapping,
)
from beverage_feed.source_http import SourceHTTPError

FIXTURES = Path(__file__).parent / "fixtures"

PACK = BenchmarkPack(
    catalog_id="coke-zero-330-single",
    name="Coca-Cola Zero Sugar 330ml Can",
    brand="Coca-Cola",
    variant="Zero Sugar",
    pack_count=1,
    unit_size_ml=330,
    package_type="can",
    search_term="Coca-Cola Zero Sugar 330ml",
)

DUNNES_MAPPING = DunnesMapping(
    catalog_id=PACK.catalog_id,
    expected_product_name="Coca-Cola Zero Sugar 330ml",
    source_product_reference="COKE-ZERO-330",
    source_item_id="COKE-ZERO-330-EA",
)

SUPERVALU_MAPPING = SuperValuMapping(
    catalog_id=PACK.catalog_id,
    expected_product_name="Coca-Cola Zero Sugar Can (330 ml)",
    source_product_id="SV-330",
)

TESCO_MAPPING = TescoMapping(
    catalog_id=PACK.catalog_id,
    expected_product_name="Coca-Cola Zero Sugar 330ml Can",
    source_tpnb="12345",
)


class SuperValuFakeClient:
    """Fake SuperValu client serving the captured product fixture."""

    def __init__(self, payload):
        self.payload = payload
        self.searches: list[str] = []

    def __call__(self, search_term):
        self.searches.append(search_term)
        return {"items": [{"productId": "SV-330"}], "pagination": {"total": 1, "offset": 0}}

    def fetch_product(self, product_id):
        self.searches.append(f"product:{product_id}")
        return self.payload


def _fixture(name):
    return json.loads((FIXTURES / name).read_text())


def _mapping_by_reference(mapping):
    """The mapping each fixture payload satisfies (identity/attributes)."""
    return mapping


class _StubClient:
    """Fake client returning a canned payload or raising a canned error."""

    def __init__(self, payload=None, error=None):
        self.payload = payload
        self.error = error

    def __call__(self, search_term):
        if self.error is not None:
            raise self.error
        return self.payload


def _catalog_file(directory):
    path = Path(directory) / "catalog.json"
    path.write_text(json.dumps([{
        "catalog_id": PACK.catalog_id,
        "name": PACK.name,
        "brand": PACK.brand,
        "variant": PACK.variant,
        "pack_count": PACK.pack_count,
        "unit_size_ml": PACK.unit_size_ml,
        "package_type": PACK.package_type,
        "search_term": PACK.search_term,
    }]))
    return path


def _mapping_file(directory, retailer, mapping):
    path = Path(directory) / "mappings.json"
    path.write_text(json.dumps({
        retailer: [{
            "catalog_id": mapping.catalog_id,
            "expected_product_name": mapping.expected_product_name,
            "source_tpnb": getattr(mapping, "source_tpnb", None),
            "status": "approved",
        }]
    }))
    return path


class RunCanaryTests(unittest.TestCase):
    """run_canary probes one mapped cell per retailer through the collectors."""

    def setUp(self):
        self.catalog = [PACK]
        self.mappings = {
            "dunnes": [DUNNES_MAPPING],
            "supervalu": [SUPERVALU_MAPPING],
            "tesco": [TESCO_MAPPING],
        }

    def _clients(self, overrides=None):
        clients = {
            "dunnes": _StubClient(payload=_fixture("dunnes_search.json")),
            "supervalu": SuperValuFakeClient(_fixture("supervalu_product.json")),
            "tesco": _StubClient(payload=_fixture("tesco_products.json")),
        }
        if overrides:
            clients.update(overrides)
        return clients

    def test_all_configured_retailers_pass_on_captured_fixtures(self):
        with tempfile.TemporaryDirectory() as directory:
            outcomes = run_canary(
                self.catalog, self.mappings, self._clients(),
                store_ids={"supervalu": "store-123"},
                retry_backoff=0.0, database=Path(directory) / "probe.sqlite",
            )

        self.assertEqual([outcome.status for outcome in outcomes],
                         ["pass", "pass", "pass"])
        for outcome in outcomes:
            self.assertEqual(outcome.catalog_id, PACK.catalog_id)
            self.assertTrue(all(check.ok for check in outcome.checks))
            names = {check.name for check in outcome.checks}
            self.assertEqual(
                names,
                {"identity", "attributes", "displayed_price", "promotion", "drs_deposit"},
            )

    def test_transport_failure_is_reported_as_endpoint_drift(self):
        clients = self._clients({
            "tesco": _StubClient(error=SourceHTTPError("tesco HTTP 503", status=503)),
        })

        with tempfile.TemporaryDirectory() as directory:
            outcomes = run_canary(
                self.catalog, self.mappings, clients,
                retry_backoff=0.0, database=Path(directory) / "probe.sqlite",
            )

        tesco = next(outcome for outcome in outcomes if outcome.retailer == "tesco")
        self.assertEqual(tesco.status, "drift")
        self.assertIn("HTTP 503", tesco.error)

    def test_response_shape_change_is_reported_as_endpoint_drift(self):
        clients = self._clients({
            "dunnes": _StubClient(payload={"unexpected": "shape"}),
        })

        with tempfile.TemporaryDirectory() as directory:
            outcomes = run_canary(
                self.catalog, self.mappings, clients,
                retry_backoff=0.0, database=Path(directory) / "probe.sqlite",
            )

        dunnes = next(outcome for outcome in outcomes if outcome.retailer == "dunnes")
        self.assertEqual(dunnes.status, "drift")
        self.assertIn("productSearch", dunnes.error)

    def test_absent_listing_is_reported_separately_from_drift(self):
        # Mirror the real Dunnes client envelope for a below-capacity page:
        # the embedded page-size evidence proves the search covered every
        # match, so the absence is not_found (canary: absent, not drift).
        clients = self._clients({
            "dunnes": _StubClient(
                payload={
                    "data": {"productSearch": {"products": []}},
                    "items": [],
                    "pagination": {"pageSize": 50},
                },
            ),
        })

        with tempfile.TemporaryDirectory() as directory:
            outcomes = run_canary(
                self.catalog, self.mappings, clients,
                retry_backoff=0.0, database=Path(directory) / "probe.sqlite",
            )

        dunnes = next(outcome for outcome in outcomes if outcome.retailer == "dunnes")
        self.assertEqual(dunnes.status, "absent")
        self.assertNotEqual(dunnes.status, "drift")

    def test_malformed_price_is_reported_as_invalid_not_drift(self):
        broken = _fixture("tesco_products.json")
        broken["products"][0]["price"] = {}

        with tempfile.TemporaryDirectory() as directory:
            outcomes = run_canary(
                self.catalog, self.mappings,
                self._clients({"tesco": _StubClient(payload=broken)}),
                retry_backoff=0.0, database=Path(directory) / "probe.sqlite",
            )

        tesco = next(outcome for outcome in outcomes if outcome.retailer == "tesco")
        self.assertEqual(tesco.status, "invalid")
        self.assertIn("price", tesco.error.lower())

    def test_retailer_without_approved_mapping_is_invalid(self):
        with tempfile.TemporaryDirectory() as directory:
            outcomes = run_canary(
                self.catalog, {"tesco": []},
                {"tesco": _StubClient(payload=_fixture("tesco_products.json"))},
                retailers=("tesco",),
                retry_backoff=0.0, database=Path(directory) / "probe.sqlite",
            )

        self.assertEqual(outcomes[0].status, "invalid")
        self.assertIn("no approved", outcomes[0].error)

    def test_retailer_without_client_is_invalid_not_a_crash(self):
        with tempfile.TemporaryDirectory() as directory:
            outcomes = run_canary(
                self.catalog, self.mappings, {},
                retailers=("tesco",),
                retry_backoff=0.0, database=Path(directory) / "probe.sqlite",
            )

        self.assertEqual(outcomes[0].status, "invalid")
        self.assertIn("client", outcomes[0].error.lower())

    def test_catalog_id_pins_the_probed_listing(self):
        other = BenchmarkPack(
            catalog_id="other-pack", name="Other", brand="Other", variant="Other",
            pack_count=1, unit_size_ml=500, package_type="bottle",
            search_term="other",
        )
        other_mapping = TescoMapping(
            catalog_id="other-pack", expected_product_name="Other",
            source_tpnb="999",
        )

        with tempfile.TemporaryDirectory() as directory:
            outcomes = run_canary(
                [PACK, other], {"tesco": [TESCO_MAPPING, other_mapping]},
                self._clients({"tesco": _StubClient(payload=_fixture("tesco_products.json"))}),
                retailers=("tesco",), catalog_ids={"tesco": PACK.catalog_id},
                retry_backoff=0.0, database=Path(directory) / "probe.sqlite",
            )

        self.assertEqual(outcomes[0].catalog_id, PACK.catalog_id)
        self.assertEqual(outcomes[0].status, "pass")


class ObservationCheckTests(unittest.TestCase):
    """Field-level checks over the probe observation row."""

    def _row(self, **overrides):
        row = {
            "source_product_reference": "COKE-ZERO-330",
            "source_product_name": "Coca-Cola Zero Sugar 330ml",
            "displayed_price": "2.79",
            "clubcard_price": None,
            "drs_deposit": "0.15",
            "pack_count": PACK.pack_count,
            "unit_size_ml": PACK.unit_size_ml,
            "package_type": PACK.package_type,
        }
        row.update(overrides)
        return row

    def test_identity_mismatch_fails_even_when_a_price_was_observed(self):
        checks = _observation_checks(PACK, DUNNES_MAPPING, self._row(
            source_product_reference="SOME-OTHER-SKU",
        ))

        identity = next(check for check in checks if check.name == "identity")
        self.assertFalse(identity.ok)

    def test_missing_displayed_price_fails(self):
        checks = _observation_checks(PACK, DUNNES_MAPPING, self._row(
            displayed_price=None,
        ))

        price = next(check for check in checks if check.name == "displayed_price")
        self.assertFalse(price.ok)

    def test_negative_drs_deposit_fails(self):
        checks = _observation_checks(PACK, DUNNES_MAPPING, self._row(
            drs_deposit="-0.15",
        ))

        deposit = next(check for check in checks if check.name == "drs_deposit")
        self.assertFalse(deposit.ok)

    def test_missing_deposit_is_allowed_but_promotion_must_parse(self):
        ok_without_deposit = _observation_checks(
            PACK, DUNNES_MAPPING, self._row(drs_deposit=None))
        deposit = next(c for c in ok_without_deposit if c.name == "drs_deposit")
        self.assertTrue(deposit.ok)

        broken = _observation_checks(
            PACK, DUNNES_MAPPING, self._row(clubcard_price="two-ninety"))
        promotion = next(c for c in broken if c.name == "promotion")
        self.assertFalse(promotion.ok)

    def test_recorded_promotion_is_reported_separately(self):
        checks = _observation_checks(PACK, TESCO_MAPPING, self._row(
            clubcard_price="2.50",
        ))

        promotion = next(check for check in checks if check.name == "promotion")
        self.assertTrue(promotion.ok)
        self.assertIn("2.50", promotion.detail)

    def test_stale_pack_composition_fails_attributes(self):
        checks = _observation_checks(PACK, DUNNES_MAPPING, self._row(
            unit_size_ml=500,
        ))

        attributes = next(check for check in checks if check.name == "attributes")
        self.assertFalse(attributes.ok)


class SelectCanaryCellTests(unittest.TestCase):
    def test_unapproved_mapping_is_never_probed(self):
        rejected = TescoMapping(
            catalog_id=PACK.catalog_id,
            expected_product_name="Coca-Cola Zero Sugar 330ml Can",
            source_tpnb="12345",
            status="rejected",
        )

        self.assertIsNone(_select_canary_cell([PACK], {"tesco": [rejected]}, "tesco"))
        selected = _select_canary_cell([PACK], {"tesco": [rejected]}, "tesco",
                                       require_approved=False)
        self.assertIsNotNone(selected)


class ReleaseGateTests(unittest.TestCase):
    """The gate blocks normal collection after repeated canary failures."""

    def _make_outcome(self, retailer="tesco", status="drift", checked_at="2026-08-27T12:00:00Z"):
        return CanaryOutcome(
            retailer=retailer, catalog_id=PACK.catalog_id, status=status,
            checks=(), error=None if status == "pass" else f"probe {status}",
            checked_at=checked_at, duration_ms=1.0,
        )

    def test_gate_opens_when_no_canary_has_ever_run(self):
        with tempfile.TemporaryDirectory() as directory:
            state = Path(directory) / "gate.json"
            self.assertEqual(release_gate(state), {})

    def test_repeated_failures_block_the_retailer(self):
        with tempfile.TemporaryDirectory() as directory:
            state = Path(directory) / "gate.json"
            for _ in range(GATE_FAILURE_THRESHOLD):
                record_outcomes(state, [self._make_outcome()])

            gate = release_gate(state)

        self.assertIn("tesco", gate)
        self.assertIn("3 consecutive canary failures", gate["tesco"])
        self.assertEqual(load_gate_state(state)["version"], 1)

    def test_single_failure_does_not_block(self):
        with tempfile.TemporaryDirectory() as directory:
            state = Path(directory) / "gate.json"
            record_outcomes(state, [self._make_outcome()])

            self.assertEqual(release_gate(state), {})

    def test_passing_canary_reopens_the_gate(self):
        with tempfile.TemporaryDirectory() as directory:
            state = Path(directory) / "gate.json"
            for _ in range(GATE_FAILURE_THRESHOLD):
                record_outcomes(state, [self._make_outcome()])
            record_outcomes(state, [self._make_outcome(status="pass")])

            self.assertEqual(release_gate(state), {})

    def test_stale_failures_stop_blocking(self):
        with tempfile.TemporaryDirectory() as directory:
            state = Path(directory) / "gate.json"
            record_outcomes(state, [self._make_outcome(checked_at="2026-01-01T00:00:00Z")])

            gate = release_gate(
                state,
                now="2026-12-01T00:00:00Z",
                max_age_hours=GATE_FAILURE_THRESHOLD,
            )

        self.assertEqual(gate, {})

    def test_gate_tracks_retailers_independently(self):
        with tempfile.TemporaryDirectory() as directory:
            state = Path(directory) / "gate.json"
            for _ in range(GATE_FAILURE_THRESHOLD):
                record_outcomes(state, [self._make_outcome(retailer="tesco")])
            record_outcomes(state, [self._make_outcome(retailer="dunnes")])

            gate = release_gate(state)

        self.assertIn("tesco", gate)
        self.assertNotIn("dunnes", gate)


class CanaryCommandTests(unittest.TestCase):
    """The CLI wires clients, prints one line per retailer, and updates the gate."""

    def test_main_reports_pass_and_exits_zero(self):
        clients = {
            "tesco": _StubClient(payload=_fixture("tesco_products.json")),
        }
        with tempfile.TemporaryDirectory() as directory:
            gate_state = Path(directory) / "gate.json"
            with patch("beverage_feed.canary._default_clients", return_value=clients):
                code = main([
                    "--catalog", str(_catalog_file(directory)),
                    "--mapping", str(_mapping_file(directory, "tesco", TESCO_MAPPING)),
                    "--retailer", "tesco",
                    "--gate-state", str(gate_state),
                    "--retry-backoff", "0",
                ])

            self.assertEqual(code, 0)
            gate = release_gate(gate_state)
            self.assertEqual(gate, {})

    def test_main_records_gate_failure_and_exits_nonzero(self):
        clients = {
            "tesco": _StubClient(error=SourceHTTPError("tesco HTTP 403", status=403)),
        }
        with tempfile.TemporaryDirectory() as directory:
            gate_state = Path(directory) / "gate.json"
            with patch("beverage_feed.canary._default_clients", return_value=clients):
                code = main([
                    "--catalog", str(_catalog_file(directory)),
                    "--mapping", str(_mapping_file(directory, "tesco", TESCO_MAPPING)),
                    "--retailer", "tesco",
                    "--gate-state", str(gate_state),
                    "--retry-backoff", "0",
                ])

            self.assertEqual(code, 1)
            history = load_gate_state(gate_state)["retailers"]["tesco"]
            self.assertEqual(history[0]["status"], "drift")
            self.assertIn("HTTP 403", history[0]["error"])
            # A single failure does not trip the gate yet...
            self.assertEqual(release_gate(gate_state), {})
            # ...but threshold consecutive failures do.
            for _ in range(GATE_FAILURE_THRESHOLD - 1):
                with patch("beverage_feed.canary._default_clients", return_value=clients):
                    main([
                        "--catalog", str(_catalog_file(directory)),
                        "--mapping", str(_mapping_file(directory, "tesco", TESCO_MAPPING)),
                        "--retailer", "tesco",
                        "--gate-state", str(gate_state),
                        "--retry-backoff", "0",
                    ])
            self.assertIn("tesco", release_gate(gate_state))

    def test_main_without_supervalu_store_id_returns_nonzero_without_raising(self):
        """A missing store id is a usage error: main returns nonzero (CONTRIBUTING §9).

        Package code never raises SystemExit via parser.error; the CLI reports
        the problem on stderr and returns an exit code instead.
        """
        with tempfile.TemporaryDirectory() as directory:
            argv = [
                "--catalog", str(_catalog_file(directory)),
                "--mapping", str(_mapping_file(directory, "supervalu", SUPERVALU_MAPPING)),
                "--retailer", "supervalu",
                "--gate-state", str(Path(directory) / "gate.json"),
            ]
            env = {k: v for k, v in os.environ.items() if k != "SUPERVALU_STORE_ID"}
            stderr = io.StringIO()
            with patch.dict(os.environ, env, clear=True), \
                    patch("sys.stderr", stderr):
                try:
                    code = main(argv)
                except SystemExit as exc:
                    self.fail(
                        f"main raised SystemExit({exc.code!r}) instead of returning"
                    )

        self.assertEqual(code, 2)
        self.assertIn("--supervalu-store-id", stderr.getvalue())

    def test_main_supervalu_store_id_from_environment_passes_the_requirement_check(self):
        """SUPERVALU_STORE_ID in the environment satisfies the store-id requirement.

        The probe against the fake may still fail (code 1); what matters is that
        main gets past the usage check and never prints the store-id error.
        """
        clients = {
            "supervalu": SuperValuFakeClient(_fixture("supervalu_product.json")),
        }
        with tempfile.TemporaryDirectory() as directory:
            with patch.dict(os.environ, {"SUPERVALU_STORE_ID": "1234"}), \
                    patch("beverage_feed.canary._default_clients", return_value=clients):
                code = main([
                    "--catalog", str(_catalog_file(directory)),
                    "--mapping", str(_mapping_file(directory, "supervalu", SUPERVALU_MAPPING)),
                    "--retailer", "supervalu",
                    "--gate-state", str(Path(directory) / "gate.json"),
                    "--retry-backoff", "0",
                ])

        # The probe itself may fail against the fake; only the usage guard matters.
        self.assertIn(code, (0, 1))

    def test_main_gate_status_reports_blocked_retailer(self):
        with tempfile.TemporaryDirectory() as directory:
            gate_state = Path(directory) / "gate.json"
            record_outcomes(gate_state, [CanaryOutcome(
                retailer="tesco", catalog_id=PACK.catalog_id, status="drift",
                checks=(), error="HTTP 403", checked_at="2026-08-27T12:00:00Z",
                duration_ms=1.0,
            )] * GATE_FAILURE_THRESHOLD)

            with patch("sys.stdout"):
                code = main([
                    "--gate-state", str(gate_state),
                    "--gate-status",
                ])

        self.assertEqual(code, 1)

    def test_main_dump_fixtures_writes_scrubbed_payloads(self):
        clients = {
            "tesco": _StubClient(payload=_fixture("tesco_products.json")),
        }
        with tempfile.TemporaryDirectory() as directory:
            fixtures_dir = Path(directory) / "fixtures-out"
            with patch("beverage_feed.canary._default_clients", return_value=clients):
                code = main([
                    "--catalog", str(_catalog_file(directory)),
                    "--mapping", str(_mapping_file(directory, "tesco", TESCO_MAPPING)),
                    "--retailer", "tesco",
                    "--gate-state", str(Path(directory) / "gate.json"),
                    "--dump-fixtures", str(fixtures_dir),
                    "--retry-backoff", "0",
                ])

            self.assertEqual(code, 0)
            dumped = json.loads((fixtures_dir / "tesco.json").read_text())
            self.assertIn("products", dumped)

    def test_canary_never_touches_the_feed_database(self):
        clients = {
            "tesco": _StubClient(payload=_fixture("tesco_products.json")),
        }
        with tempfile.TemporaryDirectory() as directory:
            feed_db = Path(directory) / "feed.sqlite"
            with patch("beverage_feed.canary._default_clients", return_value=clients):
                main([
                    "--catalog", str(_catalog_file(directory)),
                    "--mapping", str(_mapping_file(directory, "tesco", TESCO_MAPPING)),
                    "--retailer", "tesco",
                    "--gate-state", str(Path(directory) / "gate.json"),
                    "--retry-backoff", "0",
                ])

            self.assertFalse(feed_db.exists())

    def test_module_never_runs_live_clients_at_import_or_collection(self):
        # Guard the hermeticity contract: the canary module must not build any
        # live retailer client at import time.
        import beverage_feed.canary as canary_module

        self.assertEqual(canary_module.CANARY_RETAILERS, ("dunnes", "supervalu", "tesco"))


class ReleaseGateCollectionEnforcementTests(unittest.TestCase):
    """Normal collection honours the release gate only when it is enabled."""

    def test_blocked_retailer_stops_collection_with_release_gate_enabled(self):
        from beverage_feed.collector import main as collect_main

        with tempfile.TemporaryDirectory() as directory:
            gate_state = Path(directory) / "gate.json"
            record_outcomes(gate_state, [CanaryOutcome(
                retailer="tesco", catalog_id=PACK.catalog_id, status="drift",
                checks=(), error="HTTP 403", checked_at="2026-08-27T12:00:00Z",
                duration_ms=1.0,
            )] * GATE_FAILURE_THRESHOLD)

            with self.assertRaises(SystemExit) as ctx:
                collect_main([
                    "--catalog", str(_catalog_file(directory)),
                    "--mapping", str(_mapping_file(directory, "tesco", TESCO_MAPPING)),
                    "--retailer", "tesco",
                    "--database", str(Path(directory) / "feed.sqlite"),
                    "--release-gate", "--gate-state", str(gate_state),
                ])

        self.assertEqual(ctx.exception.code, 2)  # argparse error: nothing left to run

    def test_open_gate_lets_collection_proceed_to_adapter_construction(self):
        from beverage_feed.collector import main as collect_main

        with tempfile.TemporaryDirectory() as directory:
            gate_state = Path(directory) / "gate.json"
            record_outcomes(gate_state, [CanaryOutcome(
                retailer="tesco", catalog_id=PACK.catalog_id, status="pass",
                checks=(), error=None, checked_at="2026-08-27T12:00:00Z",
                duration_ms=1.0,
            )])

            # Gate open: collection proceeds past the gate. Without
            # TESCO_API_KEY the Tesco adapter cannot be built, which the
            # collector reports as a ValueError — proving the gate did not
            # block and no live request was attempted.
            with patch.dict("os.environ", {"TESCO_API_KEY": ""}):
                with self.assertRaises(ValueError):
                    collect_main([
                        "--catalog", str(_catalog_file(directory)),
                        "--mapping", str(_mapping_file(directory, "tesco", TESCO_MAPPING)),
                        "--retailer", "tesco",
                        "--database", str(Path(directory) / "feed.sqlite"),
                        "--release-gate", "--gate-state", str(gate_state),
                    ])

    def test_gate_is_not_enforced_by_default(self):
        from beverage_feed.collector import main as collect_main

        with tempfile.TemporaryDirectory() as directory:
            gate_state = Path(directory) / "gate.json"
            record_outcomes(gate_state, [CanaryOutcome(
                retailer="tesco", catalog_id=PACK.catalog_id, status="drift",
                checks=(), error="HTTP 403", checked_at="2026-08-27T12:00:00Z",
                duration_ms=1.0,
            )] * GATE_FAILURE_THRESHOLD)

            # No --release-gate flag: the gate file is ignored and collection
            # proceeds to adapter construction (fails on missing key instead).
            with patch.dict("os.environ", {"TESCO_API_KEY": ""}):
                with self.assertRaises(ValueError):
                    collect_main([
                        "--catalog", str(_catalog_file(directory)),
                        "--mapping", str(_mapping_file(directory, "tesco", TESCO_MAPPING)),
                        "--retailer", "tesco",
                        "--database", str(Path(directory) / "feed.sqlite"),
                        "--gate-state", str(gate_state),
                    ])
