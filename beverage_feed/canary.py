"""Live retailer canary and operator release gate (ticket audit-10).

Before trusting a scheduled or manual feed run, the operator can probe one
known mapped listing per configured retailer (Dunnes, SuperValu, Tesco) and
verify that the source still returns a valid exact-pack price: source
identity, product attributes, Displayed Price, separate promotions, and the
DRS Deposit. The canary reports **endpoint drift** (the route, auth, or
response shape broke) separately from **product absence** (the endpoint
works but the mapped listing is gone), because the two need different
remediation: route/auth work versus Catalog Mapping refresh.

This command touches live retailers by definition, so:

- It is a manually invoked CLI subcommand (``python -m beverage_feed canary``,
  ``make canary``, or the ``workflow_dispatch``-only ``canary.yml`` GitHub
  Actions workflow). It is never scheduled, never part of CI checks, and
  never run by the test suite — tests cover its logic with fake clients and
  captured fixtures.
- Probes run against a throwaway database in a temp directory; the canary
  never writes Price Observations, collection runs, or mappings to the real
  feed. Only the small release-gate state file (``--gate-state``, default
  ``data/canary-gate.json``) records the outcomes.

**Release gate.** ``record_outcomes`` appends every canary outcome per
retailer; ``release_gate`` reports a retailer as blocked once
``GATE_FAILURE_THRESHOLD`` consecutive canary runs failed and the newest
outcome is within ``GATE_MAX_AGE_HOURS``. ``python -m beverage_feed
--release-gate`` (or ``DRINKS_RELEASE_GATE=1``) consults the gate before
collection and refuses blocked retailers, so a drifting source cannot poison
the feed while the operator is unaware. The gate is opt-in so the scheduled
GitHub Actions collection (a disposable database, no canary state) is never
surprised by it.

**Refreshing captured fixtures and Catalog Mappings.** Run the canary with
``--dump-fixtures DIR`` to write the scrubbed, client-normalized response
payload per retailer. Trim a payload to the mapped listing, save it over the
matching ``tests/fixtures/<retailer>_*.json`` file, run pytest, and commit.
When the listing's source identity changed, update ``data/mappings.json``
(the Catalog Mapping) in the same commit — the canary's ``identity`` check
pins the mapping's source reference against the observed listing.

**SuperValu store scope and Tesco API key.** SuperValu probes run against the
same configured store as collection (``--supervalu-store-id`` or
``SUPERVALU_STORE_ID``); the store identifier is recorded as the result's
source scope, so a canary pass is only valid for that store. Tesco requires
``TESCO_API_KEY`` (sent as the ``x-apikey`` GraphQL header); without it the
Tesco canary reports ``invalid`` ("not configured") rather than pretending
the route is healthy.
"""

from __future__ import annotations

import argparse
import json
import os
import sqlite3
import sys
import tempfile
import time
import uuid
from contextlib import closing
from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any, Mapping, Sequence

from .collector import (
    BenchmarkPack,
    SuperValuClient,
    TescoClient,
    DunnesClient,
    _find_listing,
    _find_supervalu_listing,
    _find_tesco_listing,
    _load_mappings,
    _retrying_fetcher,
    _validate_listing,
    as_datetime,
    collect_one,
    collect_supervalu_one,
    collect_tesco_one,
    ensure_schema,
    load_catalog,
    safe_record,
    timestamp,
)

#: Retailers the canary probes by default (ticket audit-10 scope).
CANARY_RETAILERS = ("dunnes", "supervalu", "tesco")

#: Canary outcome: the mapped listing was observed and every check passed.
STATUS_PASS = "pass"
#: Canary outcome: the route/auth/response shape broke (fix the source route).
STATUS_DRIFT = "drift"
#: Canary outcome: the endpoint works but the mapped listing is gone
#: (refresh the Catalog Mapping).
STATUS_ABSENT = "absent"
#: Canary outcome: source answered but validation failed (identity,
#: attributes, price, promotion, or deposit).
STATUS_INVALID = "invalid"

#: Consecutive canary failures that trip the release gate for a retailer.
GATE_FAILURE_THRESHOLD = 3
#: A gate older than this stops blocking; the operator must re-run the canary.
GATE_MAX_AGE_HOURS = 168
#: Gate state file retained per retailer (newest first).
GATE_HISTORY_LIMIT = 50
#: Current gate state file layout version.
GATE_STATE_VERSION = 1

#: Default release-gate state path (override with --gate-state or
#: DRINKS_CANARY_STATE).
DEFAULT_GATE_STATE = Path(os.environ.get("DRINKS_CANARY_STATE", "data/canary-gate.json"))

_GATE_STATE_VERSION_KEY = "version"
_GATE_RETAILERS_KEY = "retailers"


@dataclass(frozen=True)
class CanaryCheck:
    """One named validation performed against the probed listing."""

    name: str
    ok: bool
    detail: str


@dataclass(frozen=True)
class CanaryOutcome:
    """The result of probing one retailer's mapped listing."""

    retailer: str
    catalog_id: str | None
    status: str
    checks: tuple[CanaryCheck, ...]
    error: str | None
    checked_at: str
    duration_ms: float
    raw_payload: Mapping[str, Any] | None = field(default=None, repr=False)

    @property
    def passed(self) -> bool:
        return self.status == STATUS_PASS

    def summary_line(self) -> str:
        """Compact single-line operator summary for this outcome."""
        ok = sum(1 for check in self.checks if check.ok)
        line = (
            f"canary {self.retailer}: {self.status}"
            f" catalog={self.catalog_id or '-'}"
            f" checks={ok}/{len(self.checks)}"
            f" duration_ms={self.duration_ms}"
        )
        if self.error:
            line += f" [{self.error}]"
        return line

    def to_record(self) -> dict[str, Any]:
        """JSON-serializable gate-history record (no raw payloads)."""
        return {
            "checked_at": self.checked_at,
            "status": self.status,
            "catalog_id": self.catalog_id,
            "error": self.error,
            "duration_ms": self.duration_ms,
            "checks": [
                {"name": check.name, "ok": check.ok, "detail": check.detail}
                for check in self.checks
            ],
        }


class _CapturingClient:
    """Wrap a retailer client, capturing the payload or the first failure.

    The canary reuses the real collectors, which swallow fetch exceptions
    into ``source_error`` results; the capture keeps the raised exception and
    the last successful payload so the canary can classify the failure
    without issuing a second live request.
    """

    def __init__(self, inner: Any) -> None:
        self._inner = inner
        self.payload: Mapping[str, Any] | None = None
        self.error: Exception | None = None

    def __call__(self, search_term: str) -> Mapping[str, Any]:
        return self._capture(lambda: self._inner(search_term))

    def fetch_product(self, reference: str) -> Mapping[str, Any]:
        return self._capture(lambda: self._inner.fetch_product(reference))

    def _capture(self, fetch: Any) -> Mapping[str, Any]:
        try:
            payload = fetch()
        except Exception as exc:
            if self.error is None:
                self.error = exc
            raise
        self.payload = payload
        return payload


def _mapping_source_reference(mapping: Any) -> str | None:
    """The retailer source identifier pinned by a Catalog Mapping, if any."""
    for attr in ("source_product_reference", "source_product_id", "source_tpnb"):
        value = getattr(mapping, attr, None)
        if value:
            return str(value)
    return None


def _select_canary_cell(
    catalog: list[BenchmarkPack],
    mappings: Mapping[str, Sequence[Any]],
    retailer: str,
    catalog_id: str | None = None,
    *,
    require_approved: bool = True,
) -> tuple[BenchmarkPack, Any] | None:
    """Pick the mapped listing the canary probes for one retailer.

    Prefers the first approved Catalog Mapping that has a matching catalog
    pack; ``catalog_id`` pins an exact cell. Returns ``None`` when no cell
    qualifies.
    """
    packs = {pack.catalog_id: pack for pack in catalog}
    for mapping in mappings.get(retailer, ()):
        if require_approved and getattr(mapping, "status", "approved") != "approved":
            continue
        pack = packs.get(mapping.catalog_id)
        if pack is None:
            continue
        if catalog_id is not None and mapping.catalog_id != catalog_id:
            continue
        return pack, mapping
    return None


def _collect_probe(
    retailer: str,
    pack: BenchmarkPack,
    mapping: Any,
    fetcher: Any,
    database: Path,
    *,
    store_id: str | None = None,
    run_id: str | None = None,
    started_at: str | None = None,
) -> dict[str, Any]:
    """Run the retailer's real collector against the probe fetcher."""
    if retailer == "dunnes":
        return collect_one(
            pack, mapping, fetcher, database,
            _run_id=run_id, _started_at=started_at,
        )
    if retailer == "supervalu":
        return collect_supervalu_one(
            pack, mapping, fetcher, database, store_id=store_id,
            _run_id=run_id, _started_at=started_at,
        )
    if retailer == "tesco":
        return collect_tesco_one(
            pack, mapping, fetcher, database,
            _run_id=run_id, _started_at=started_at,
        )
    raise ValueError(f"unsupported canary retailer: {retailer}")


def _replay_find(retailer: str, payload: Mapping[str, Any], mapping: Any) -> Any:
    """Re-run the collector's listing lookup on the captured payload.

    Distinguishes a response-shape change (``ValueError`` from the collector's
    find helper — endpoint drift) from a genuinely absent listing
    (``LookupError`` — product absence) without a second live request.
    """
    if retailer == "dunnes":
        return _find_listing(payload, mapping)
    if retailer == "supervalu":
        items = payload.get("items")
        if isinstance(items, list):
            return _find_supervalu_listing(payload, mapping)
        # Direct-hydration shape: the payload is the product itself.
        if isinstance(payload, Mapping) and payload.get("productId"):
            return payload
        raise ValueError("SuperValu response has no items list")
    if retailer == "tesco":
        return _find_tesco_listing(payload, mapping)
    raise ValueError(f"unsupported canary retailer: {retailer}")


def _observation_checks(
    pack: BenchmarkPack,
    mapping: Any,
    row: Mapping[str, Any],
) -> tuple[CanaryCheck, ...]:
    """Validate the probe observation: identity, attributes, money fields."""
    checks: list[CanaryCheck] = []

    # 1. Source identity: the observed listing must be the mapped listing.
    expected_ref = _mapping_source_reference(mapping)
    observed_ref = str(row.get("source_product_reference") or "")
    if expected_ref is None:
        checks.append(CanaryCheck(
            "identity", True,
            "mapping pins no source reference; listing matched by expected name",
        ))
    elif observed_ref == expected_ref:
        checks.append(CanaryCheck(
            "identity", True, f"source reference {observed_ref} matches the mapping",
        ))
    else:
        checks.append(CanaryCheck(
            "identity", False,
            f"mapping pins {expected_ref} but the source answered {observed_ref!r}",
        ))

    # 2. Product attributes: the listing must still be the exact pack.
    name = str(row.get("source_product_name") or "")
    reason = _validate_listing(name, pack)
    composition_ok = (
        row.get("pack_count") == pack.pack_count
        and row.get("unit_size_ml") == pack.unit_size_ml
        and (row.get("package_type") or pack.package_type) == pack.package_type
    )
    if reason is None and composition_ok:
        checks.append(CanaryCheck(
            "attributes", True,
            f"{name!r} matches {pack.brand} {pack.variant}"
            f" {pack.pack_count}x{pack.unit_size_ml}ml {pack.package_type}",
        ))
    else:
        detail = reason or (
            f"composition drifted: source {row.get('pack_count')}x"
            f"{row.get('unit_size_ml')}ml {row.get('package_type')!r}"
            f" vs catalog {pack.pack_count}x{pack.unit_size_ml}ml"
            f" {pack.package_type!r}"
        )
        checks.append(CanaryCheck("attributes", False, detail))

    # 3. Displayed Price: present, parseable, positive.
    displayed = row.get("displayed_price")
    try:
        price = Decimal(str(displayed)) if displayed is not None else None
        if price is None or price <= 0:
            raise InvalidOperation
        checks.append(CanaryCheck(
            "displayed_price", True, f"valid displayed price {price}",
        ))
    except (InvalidOperation, TypeError, ValueError):
        checks.append(CanaryCheck(
            "displayed_price", False,
            f"no valid displayed price recorded (raw: {displayed!r})",
        ))

    # 4. Promotions: a loyalty/promotion price must be recorded separately
    #    from the Displayed Price, and must be valid money when present.
    promotion = row.get("clubcard_price")
    if promotion is None:
        checks.append(CanaryCheck(
            "promotion", True, "no separate promotion price recorded",
        ))
    else:
        try:
            promo_value = Decimal(str(promotion))
            if promo_value < 0:
                raise InvalidOperation
            checks.append(CanaryCheck(
                "promotion", True,
                f"promotion price {promo_value} recorded separately"
                " from the displayed price",
            ))
        except (InvalidOperation, TypeError, ValueError):
            checks.append(CanaryCheck(
                "promotion", False,
                f"malformed promotion price recorded (raw: {promotion!r})",
            ))

    # 5. DRS Deposit: absent evidence is allowed (recorded as NULL), but a
    #    recorded deposit must be valid, non-negative money.
    deposit = row.get("drs_deposit")
    if deposit is None:
        checks.append(CanaryCheck(
            "drs_deposit", True, "no DRS deposit evidence recorded (NULL)",
        ))
    else:
        try:
            deposit_value = Decimal(str(deposit))
            if deposit_value < 0:
                raise InvalidOperation
            checks.append(CanaryCheck(
                "drs_deposit", True, f"valid DRS deposit {deposit_value}",
            ))
        except (InvalidOperation, TypeError, ValueError):
            checks.append(CanaryCheck(
                "drs_deposit", False,
                f"malformed DRS deposit recorded (raw: {deposit!r})",
            ))

    return tuple(checks)


def _probe_observation_row(
    database: Path, retailer: str, catalog_id: str
) -> dict[str, Any] | None:
    """Read the probe's Price Observation row back from the scratch database."""
    with closing(sqlite3.connect(database)) as connection:
        connection.row_factory = sqlite3.Row
        row = connection.execute(
            """
            SELECT * FROM price_observations
            WHERE retailer = ? AND catalog_id = ?
            ORDER BY observation_id DESC LIMIT 1
            """,
            (retailer, catalog_id),
        ).fetchone()
    return dict(row) if row is not None else None


def _classify_result(
    retailer: str,
    pack: BenchmarkPack,
    mapping: Any,
    result: Mapping[str, Any],
    captured: _CapturingClient,
    probe_database: Path,
) -> tuple[str, str | None, tuple[CanaryCheck, ...]]:
    """Map a probe collection result onto a canary status.

    Endpoint drift (transport/HTTP/auth/shape failures and truncated pages)
    is reported separately from product absence (a healthy route whose mapped
    listing is gone) and from invalid responses (identity, attribute, or
    money validation failures).
    """
    status = result.get("status")
    error = result.get("error")

    if status == "observed":
        assert pack.catalog_id is not None
        row = _probe_observation_row(probe_database, retailer, pack.catalog_id)
        if row is None:
            return (
                STATUS_INVALID,
                "collector reported observed but recorded no Price Observation",
                (),
            )
        checks = _observation_checks(pack, mapping, row)
        failed = next((check for check in checks if not check.ok), None)
        if failed is not None:
            return STATUS_INVALID, failed.detail, checks
        return STATUS_PASS, None, checks

    if status == "not_found":
        return STATUS_ABSENT, error or "mapped listing was not found", ()

    if status == "inconclusive":
        # The page could not prove it covered every match (audit-06): the
        # route answered but not healthily.
        return STATUS_DRIFT, error or "source page was inconclusive", ()

    if status == "unmapped":
        return STATUS_INVALID, error or "catalog mapping is not approved", ()

    # status == "source_error": either the fetch failed (drift) or the source
    # answered and validation/price parsing failed (invalid).
    if captured.error is not None:
        return STATUS_DRIFT, f"source request failed after retries: {captured.error}", ()
    payload = captured.payload
    if payload is None:
        return STATUS_DRIFT, error or "source request produced no payload", ()
    try:
        _replay_find(retailer, payload, mapping)
    except LookupError:
        return STATUS_ABSENT, error or "mapped listing was not found", ()
    except ValueError as exc:
        return STATUS_DRIFT, f"response shape changed: {exc}", ()
    return STATUS_INVALID, error or "source response failed validation", ()


def _probe_retailer(
    retailer: str,
    catalog: list[BenchmarkPack],
    mappings: Mapping[str, Sequence[Any]],
    clients: Mapping[str, Any],
    *,
    catalog_id: str | None,
    store_ids: Mapping[str, str] | None,
    max_retries: int,
    retry_backoff: float,
    database: Path | None,
) -> CanaryOutcome:
    """Probe one retailer's mapped listing and classify the outcome."""
    started = time.monotonic()
    checked_at = timestamp()

    cell = _select_canary_cell(catalog, mappings, retailer, catalog_id)
    if cell is None:
        return CanaryOutcome(
            retailer=retailer, catalog_id=catalog_id, status=STATUS_INVALID,
            checks=(), error=(
                f"no approved Catalog Mapping for {retailer}"
                + (f" matching catalog_id {catalog_id}" if catalog_id else "")
                + "; refresh mappings.json first"
            ),
            checked_at=checked_at, duration_ms=0.0,
        )
    pack, mapping = cell

    client = clients.get(retailer)
    if client is None:
        return CanaryOutcome(
            retailer=retailer, catalog_id=pack.catalog_id, status=STATUS_INVALID,
            checks=(), error=(
                f"no {retailer} client configured (credentials or store scope missing)"
            ),
            checked_at=checked_at, duration_ms=0.0,
        )

    store_id = (store_ids or {}).get(retailer) or getattr(client, "store_id", None)

    # Probes write their observation into a throwaway database; the canary
    # never touches the real feed.
    if database is not None:
        probe_database = database
        scratch: tempfile.TemporaryDirectory[str] | None = None
    else:
        scratch = tempfile.TemporaryDirectory(prefix="beverage-feed-canary-")
        probe_database = Path(scratch.name) / "probe.sqlite"
    try:
        run_id = f"canary-{uuid.uuid4().hex}"
        captured = _CapturingClient(client)
        direct = (
            captured.fetch_product if hasattr(client, "fetch_product") else None
        )
        # The retrying fetcher records request diagnostics before the
        # collector creates its own run row, so seed a probe run row first
        # (collection_diagnostics.collection_runs foreign key) and hand the
        # collectors the existing run id so they never write a second one.
        with closing(sqlite3.connect(probe_database)) as connection:
            ensure_schema(connection)
            connection.execute(
                "INSERT OR IGNORE INTO collection_runs"
                " (run_id, started_at, finished_at, status, observed_count,"
                "  failed_count, summary) VALUES (?, ?, ?, 'running', 0, 0, '{}')",
                (run_id, checked_at, timestamp()),
            )
            connection.commit()
        fetcher = _retrying_fetcher(
            captured,
            database=probe_database,
            run_id=run_id,
            retailer=retailer,
            catalog_id=pack.catalog_id,
            max_retries=max_retries,
            retry_backoff=retry_backoff,
            direct_fetcher=direct,
        )
        try:
            result = _collect_probe(
                retailer, pack, mapping, fetcher, probe_database, store_id=store_id,
                run_id=run_id, started_at=checked_at,
            )
        except Exception as exc:
            return CanaryOutcome(
                retailer=retailer, catalog_id=pack.catalog_id, status=STATUS_DRIFT,
                checks=(), error=f"canary probe failed: {exc}",
                checked_at=checked_at,
                duration_ms=round((time.monotonic() - started) * 1000, 1),
            )
        status, error, checks = _classify_result(
            retailer, pack, mapping, result, captured, probe_database,
        )
        raw_payload = captured.payload
        return CanaryOutcome(
            retailer=retailer, catalog_id=pack.catalog_id, status=status,
            checks=checks, error=error, checked_at=checked_at,
            duration_ms=round((time.monotonic() - started) * 1000, 1),
            raw_payload=raw_payload,
        )
    finally:
        if scratch is not None:
            scratch.cleanup()


def run_canary(
    catalog: list[BenchmarkPack],
    mappings: Mapping[str, Sequence[Any]],
    clients: Mapping[str, Any],
    *,
    retailers: Sequence[str] = CANARY_RETAILERS,
    catalog_ids: Mapping[str, str] | None = None,
    store_ids: Mapping[str, str] | None = None,
    max_retries: int = 1,
    retry_backoff: float = 0.5,
    database: Path | None = None,
) -> list[CanaryOutcome]:
    """Probe one known mapped listing per retailer and classify the outcomes.

    ``clients`` maps retailer name to a live (or, in tests, fake) retailer
    client; a missing client yields an ``invalid`` outcome for that retailer
    rather than a crash. ``database`` optionally pins the throwaway probe
    database; by default each probe uses a temp directory.
    """
    outcomes: list[CanaryOutcome] = []
    for retailer in retailers:
        outcomes.append(_probe_retailer(
            retailer,
            catalog,
            mappings,
            clients,
            catalog_id=(catalog_ids or {}).get(retailer),
            store_ids=store_ids,
            max_retries=max_retries,
            retry_backoff=retry_backoff,
            database=database,
        ))
    return outcomes


def load_gate_state(state_path: str | Path) -> dict[str, Any]:
    """Load the release-gate state file (empty state when absent/corrupt)."""
    path = Path(state_path)
    if not path.is_file():
        return {
            _GATE_STATE_VERSION_KEY: GATE_STATE_VERSION,
            _GATE_RETAILERS_KEY: {},
        }
    try:
        state = json.loads(path.read_text())
    except (json.JSONDecodeError, OSError):
        return {
            _GATE_STATE_VERSION_KEY: GATE_STATE_VERSION,
            _GATE_RETAILERS_KEY: {},
        }
    if not isinstance(state, dict) or not isinstance(
        state.get(_GATE_RETAILERS_KEY), dict
    ):
        return {
            _GATE_STATE_VERSION_KEY: GATE_STATE_VERSION,
            _GATE_RETAILERS_KEY: {},
        }
    state.setdefault(_GATE_STATE_VERSION_KEY, GATE_STATE_VERSION)
    return state


def _write_gate_state(state_path: str | Path, state: Mapping[str, Any]) -> None:
    """Atomically persist the gate state (stable decision-file formatting)."""
    path = Path(state_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = json.dumps(state, indent=2, sort_keys=True) + "\n"
    tmp_path = path.with_suffix(path.suffix + ".tmp")
    tmp_path.write_text(payload)
    os.replace(tmp_path, path)


def record_outcomes(
    state_path: str | Path,
    outcomes: Sequence[CanaryOutcome],
) -> dict[str, Any]:
    """Append canary outcomes to the gate state (newest first, bounded)."""
    state = load_gate_state(state_path)
    retailers: dict[str, Any] = state[_GATE_RETAILERS_KEY]
    for outcome in outcomes:
        history = retailers.setdefault(outcome.retailer, [])
        if not isinstance(history, list):
            retailers[outcome.retailer] = history = []
        history.insert(0, outcome.to_record())
        del history[GATE_HISTORY_LIMIT:]
    _write_gate_state(state_path, state)
    return state


def release_gate(
    state_path: str | Path,
    *,
    now: str | None = None,
    threshold: int = GATE_FAILURE_THRESHOLD,
    max_age_hours: int = GATE_MAX_AGE_HOURS,
) -> dict[str, str]:
    """Retailers the release gate currently blocks, with reasons.

    A retailer is blocked once ``threshold`` consecutive canary runs failed
    (``status != pass``) and the newest outcome is within ``max_age_hours``.
    A gate older than that stops blocking so collection never hinges on a
    stale probe — the documented procedure is to re-run the canary, which
    refreshes (and re-trips) the gate while the source is still broken. A
    passing canary resets the streak immediately.
    """
    state = load_gate_state(state_path)
    now_dt = as_datetime(now) if now is not None else None
    blocked: dict[str, str] = {}
    for retailer, history in state[_GATE_RETAILERS_KEY].items():
        if not isinstance(history, list) or not history:
            continue
        entries = sorted(
            (entry for entry in history if isinstance(entry, dict)),
            key=lambda entry: str(entry.get("checked_at") or ""),
            reverse=True,
        )
        newest = entries[0]
        consecutive = 0
        for entry in entries:
            if entry.get("status") == STATUS_PASS:
                break
            consecutive += 1
        if consecutive < threshold:
            continue
        if now_dt is not None:
            newest_at = as_datetime(newest.get("checked_at"))
            if (now_dt - newest_at).total_seconds() > max_age_hours * 3600:
                continue
        blocked[retailer] = (
            f"blocked: {consecutive} consecutive canary failures"
            f" (latest {newest.get('status')} at {newest.get('checked_at')}"
            f": {newest.get('error') or 'no detail'});"
            " run `python -m beverage_feed canary` to re-check"
        )
    return blocked


def _default_clients(store_ids: Mapping[str, str] | None = None) -> dict[str, Any]:
    """Build the live retailer clients; a misconfigured retailer is skipped.

    Missing credentials (e.g. ``TESCO_API_KEY``) surface as an ``invalid``
    canary outcome for that retailer instead of aborting the whole probe.
    """
    clients: dict[str, Any] = {}
    try:
        clients["dunnes"] = DunnesClient()
    except ValueError as exc:
        print(f"canary: dunnes client not configured: {exc}", file=sys.stderr)
    store_id = (store_ids or {}).get("supervalu")
    try:
        clients["supervalu"] = SuperValuClient(store_id or "")
    except ValueError as exc:
        print(f"canary: supervalu client not configured: {exc}", file=sys.stderr)
    try:
        clients["tesco"] = TescoClient()
    except ValueError as exc:
        print(f"canary: tesco client not configured: {exc}", file=sys.stderr)
    return clients


def _dump_fixtures(outcomes: Sequence[CanaryOutcome], directory: Path) -> None:
    """Write scrubbed client-normalized payloads for fixture refresh.

    The captured fixture files under ``tests/fixtures/`` pin the response
    shapes the collectors consume; this writes the same shapes so an operator
    can trim them into updated fixtures (see the module docstring).
    """
    directory.mkdir(parents=True, exist_ok=True)
    for outcome in outcomes:
        if outcome.raw_payload is None:
            continue
        target = directory / f"{outcome.retailer}.json"
        target.write_text(json.dumps(safe_record(outcome.raw_payload), indent=2, sort_keys=True) + "\n")


def main(argv: list[str] | None = None) -> int:
    """CLI entry: run the live canary (never invoked by the test suite)."""
    parser = argparse.ArgumentParser(
        prog="beverage_feed canary",
        description=(
            "Probe one known mapped listing per retailer and report endpoint"
            " drift separately from product absence (manual, never scheduled)"
        ),
    )
    parser.add_argument("--catalog", type=Path, default=Path("data/catalog.json"))
    parser.add_argument("--mapping", type=Path, default=Path("data/mappings.json"))
    parser.add_argument("--retailer", choices=CANARY_RETAILERS)
    parser.add_argument("--catalog-id", help="probe exactly this catalog pack")
    parser.add_argument(
        "--supervalu-store-id",
        default=os.environ.get("SUPERVALU_STORE_ID"),
        help="configured SuperValu store identifier (or SUPERVALU_STORE_ID)",
    )
    parser.add_argument(
        "--gate-state", type=Path, default=DEFAULT_GATE_STATE,
        help="release-gate state file (default data/canary-gate.json)",
    )
    parser.add_argument(
        "--gate-status", action="store_true",
        help="print the release-gate verdict without probing; exit 1 when a retailer is blocked",
    )
    parser.add_argument(
        "--dump-fixtures", type=Path, default=None,
        help="write scrubbed response payloads here for refreshing captured fixtures",
    )
    parser.add_argument("--max-retries", type=int, default=1)
    parser.add_argument("--retry-backoff", type=float, default=0.5)
    args = parser.parse_args(argv)

    if args.gate_status:
        gate = release_gate(args.gate_state)
        for retailer in CANARY_RETAILERS:
            verdict = gate.get(retailer) or "open"
            print(f"gate {retailer}: {verdict}")
        for retailer in sorted(set(gate) - set(CANARY_RETAILERS)):
            print(f"gate {retailer}: {gate[retailer]}")
        return 1 if gate else 0

    retailers = (args.retailer,) if args.retailer else CANARY_RETAILERS
    if "supervalu" in retailers and not args.supervalu_store_id:
        parser.error("--supervalu-store-id or SUPERVALU_STORE_ID is required for SuperValu")

    catalog = load_catalog(args.catalog)
    if args.catalog_id and not any(pack.catalog_id == args.catalog_id for pack in catalog):
        raise ValueError(f"catalog pack not found: {args.catalog_id}")
    mappings = _load_mappings(args.mapping)

    clients = _default_clients({"supervalu": args.supervalu_store_id})
    catalog_ids = (
        {retailer: args.catalog_id for retailer in retailers}
        if args.catalog_id else None
    )
    outcomes = run_canary(
        catalog,
        mappings,
        clients,
        retailers=retailers,
        catalog_ids=catalog_ids,
        store_ids={"supervalu": args.supervalu_store_id},
        max_retries=args.max_retries,
        retry_backoff=args.retry_backoff,
    )

    record_outcomes(args.gate_state, outcomes)
    if args.dump_fixtures is not None:
        _dump_fixtures(outcomes, args.dump_fixtures)

    for outcome in outcomes:
        print(outcome.summary_line())
    return 0 if all(outcome.passed for outcome in outcomes) else 1


if __name__ == "__main__":
    raise SystemExit(main())
