"""Review-sprint prototype: keyboard-driven HITL mapping review (ticket 05).

A local 127.0.0.1 HTTP prototype on top of the Operator Dashboard stack that
turns discovery evidence into 15-minute review sprints.  Reads flow through
:mod:`beverage_feed.dashboard_read`; every decision is delegated to the real
seam in :mod:`beverage_feed.discovery_cli` /
:mod:`beverage_feed.discovery_decisions` (approve / reject / do-not-map /
challenge resolve / replace), so the audit trail — JSON decision files first,
then SQLite cell states, transitions, and diagnostics — is identical to the
CLI's.  This module adds no decision logic of its own.

Prototype scope (HITL, iterate with Feilim): side-by-side candidate-vs-catalog
comparison, keyboard approve/reject/exclude, batch actions, and progress
against the strict 100-pack bar fed by ``discovery_classify`` output.
"""

from __future__ import annotations

import argparse
import json
import os
import signal
import sqlite3
import socket
import sys
import threading
import traceback
import webbrowser
from contextlib import closing
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlparse

from . import dashboard_read as read
from .collector import BenchmarkPack, load_catalog, timestamp
from .dashboard import DASHBOARD_CSS, DEFAULT_HOST
from .discovery import DiscoveryStore, reconcile_json_decisions
from .discovery_adapters import normalize_listing
from .discovery_classify import classify_evidence
from .discovery_cli import (
    approve,
    do_not_map_cell,
    reject_listing,
    replace_mapping,
    review_list,
)
from .discovery_decisions import resolve_challenge
from .matching import brand_matches_alias, same_text

DEFAULT_PORT = 8766

_SPRINT_ACTIONS = ("approve", "reject", "exclude", "replace", "challenge")
_BATCH_ACTIONS = ("approve", "reject", "exclude")

# The five exact-pack attributes the Catalog Mapping bar judges, in display
# order.  Comparison rows are display-only; the decision seam re-validates.
_COMPARE_KEYS: tuple[tuple[str, str], ...] = (
    ("brand", "Brand"),
    ("variant", "Variant"),
    ("pack_count", "Pack count"),
    ("unit_size_ml", "Unit size (ml)"),
    ("package_type", "Package"),
)


class SprintApp:
    """In-process review-sprint app: workspace paths + decision delegation."""

    def __init__(
        self,
        repo_root: Path,
        *,
        database_path: Path,
        catalog_path: Path | None = None,
        mappings_path: Path | None = None,
        rejections_path: Path | None = None,
        decided_by: str = "sprint-operator",
    ) -> None:
        self.repo_root = repo_root.resolve()
        self.database_path = Path(database_path).resolve()
        self.catalog_path = Path(catalog_path) if catalog_path else self.repo_root / "data" / "catalog.json"
        self.mappings_path = Path(mappings_path) if mappings_path else self.repo_root / "data" / "mappings.json"
        self.rejections_path = Path(rejections_path) if rejections_path else self.repo_root / "data" / "rejections.json"
        self.decided_by = decided_by
        # One reconciliation at startup, like one CLI invocation: repairs an
        # interrupted JSON-first decision from a previous session.
        reconcile_json_decisions(self.database_path, self.mappings_path, self.rejections_path)
        self.catalog: dict[str, BenchmarkPack] = {
            pack.catalog_id: pack for pack in load_catalog(self.catalog_path)
        }

    def load(self) -> read.WorkspaceSnapshot:
        return read.load_workspace(
            self.repo_root,
            database_path=self.database_path,
            catalog_path=self.catalog_path,
            mappings_path=self.mappings_path,
            rejections_path=self.rejections_path,
        )

    def store(self) -> DiscoveryStore:
        return DiscoveryStore(self.database_path)


def _json_bytes(payload: Any, *, status: int = 200) -> tuple[int, bytes, str]:
    body = json.dumps(payload, default=str, indent=2).encode("utf-8")
    return status, body, "application/json; charset=utf-8"


def _html_bytes(document: str, *, status: int = 200) -> tuple[int, bytes, str]:
    return status, document.encode("utf-8"), "text/html; charset=utf-8"


def _latest_evidence(
    store: DiscoveryStore, *, retailer: str, catalog_id: str, candidate_id: str
) -> dict[str, Any] | None:
    """Candidate row plus its latest evidence row, or None when unknown."""
    with closing(store.connection()) as connection:
        connection.row_factory = sqlite3.Row
        candidate = connection.execute(
            "SELECT * FROM catalog_candidates WHERE candidate_id=?", (candidate_id,)
        ).fetchone()
        if candidate is None:
            return None
        row = connection.execute(
            "SELECT * FROM discovery_candidate_evidence "
            "WHERE candidate_id=? AND retailer=? AND catalog_id=? "
            "ORDER BY evidence_id DESC LIMIT 1",
            (candidate_id, retailer, catalog_id),
        ).fetchone()
        return {"candidate": dict(candidate), "evidence": dict(row) if row else None}


def _evidence_view(
    store: DiscoveryStore, *, retailer: str, catalog_id: str, candidate_id: str
) -> dict[str, Any]:
    """Display projection of one candidate's evidence for the comparison view.

    Mirrors ``discovery_classify._evidence_facts`` semantics (re-extract from
    the raw record when it survives, else the stored normalized evidence) so
    what the operator sees is what the classifier judged.
    """
    latest = _latest_evidence(
        store, retailer=retailer, catalog_id=catalog_id, candidate_id=candidate_id
    )
    if latest is None:
        return {"name": "", "attributes": {}, "price": None, "price_status": None,
                "identity_tier": None, "diffs": {}}
    candidate = latest["candidate"]
    row = latest["evidence"]
    name: str
    price_status: str | None
    raw_record: Any = None
    if isinstance(candidate.get("raw_record"), str) and candidate["raw_record"].strip():
        try:
            parsed = json.loads(candidate["raw_record"])
            raw_record = parsed if isinstance(parsed, dict) and parsed else None
        except ValueError:
            raw_record = None
    if raw_record is not None:
        listing = normalize_listing(retailer, raw_record)
        name = listing.name
        attributes = dict(listing.attributes)
        diffs = dict(listing.conflicts)
        price_status = listing.price.status
    else:
        def _mapping(value: Any) -> dict[str, Any]:
            if isinstance(value, str) and value.strip():
                try:
                    parsed = json.loads(value)
                except ValueError:
                    return {}
                return parsed if isinstance(parsed, dict) else {}
            return {}

        name = str(candidate.get("source_product_name") or "")
        attributes = _mapping(row["normalized_attributes"]) if row else {}
        diffs = _mapping(row["attribute_diffs"]) if row else {}
        price_status = row["price_parse_status"] if row else None
    return {
        "name": name,
        "attributes": attributes,
        "price": row["raw_price_value"] if row else None,
        "price_status": price_status,
        "identity_tier": candidate.get("identity_tier"),
        "source_product_reference": candidate.get("source_product_reference"),
        "source_item_id": candidate.get("source_item_id"),
        "diffs": diffs,
    }


def _comparison_rows(
    pack: BenchmarkPack, attributes: dict[str, Any]
) -> list[dict[str, Any]]:
    """Side-by-side attribute rows (display-only; the seam re-validates)."""
    rows: list[dict[str, Any]] = []
    for key, label in _COMPARE_KEYS:
        candidate_value = attributes.get(key)
        pack_value = getattr(pack, key)
        if key in {"brand", "variant"} and candidate_value is not None:
            match = same_text(pack_value, str(candidate_value)) or (
                key == "brand" and brand_matches_alias(pack, str(candidate_value))
            )
        else:
            match = candidate_value == pack_value
        rows.append({
            "key": key,
            "label": label,
            "candidate": candidate_value,
            "pack": pack_value,
            "match": match,
        })
    return rows


def sprint_queue(app: SprintApp) -> dict[str, Any]:
    """Sprint queue: classified candidate-cells plus open review cells."""
    store = app.store()
    classification = classify_evidence(list(app.catalog.values()), store)
    items: list[dict[str, Any]] = []
    seen: set[tuple[str, str, str]] = set()

    def add(
        retailer: str,
        catalog_id: str,
        candidate_id: str | None,
        *,
        sprint_class: str | None,
        reasons: list[str],
        review_category: str | None = None,
        cell_reason: str | None = None,
    ) -> None:
        pack = app.catalog.get(catalog_id)
        if pack is None:
            return
        key = (retailer, catalog_id, candidate_id or "")
        if key in seen:
            return
        seen.add(key)
        evidence = (
            _evidence_view(
                store, retailer=retailer, catalog_id=catalog_id, candidate_id=candidate_id
            )
            if candidate_id
            else {"name": "", "attributes": {}, "price": None,
                  "price_status": None, "identity_tier": None, "diffs": {}}
        )
        items.append({
            "retailer": retailer,
            "retailer_name": read._RETAILER_NAMES.get(retailer, retailer),
            "catalog_id": catalog_id,
            "candidate_id": candidate_id,
            "pack_name": pack.name,
            "pack": {
                "name": pack.name,
                "brand": pack.brand,
                "variant": pack.variant,
                "pack_count": pack.pack_count,
                "unit_size_ml": pack.unit_size_ml,
                "package_type": pack.package_type,
            },
            "class": sprint_class,
            "reasons": reasons,
            "review_category": review_category,
            "cell_reason": cell_reason,
            "evidence": evidence,
            "comparison": _comparison_rows(pack, evidence["attributes"]),
        })

    for sprint_class in ("A", "B", "C", "D"):
        for entry in classification["batches"][sprint_class]:
            add(
                entry["retailer"], entry["catalog_id"], entry["candidate_id"],
                sprint_class=sprint_class,
                reasons=list(entry["reasons"]),
            )
    for cell in review_list(store):
        add(
            cell["retailer"], cell["catalog_id"], cell["candidate_id"],
            sprint_class=None,
            reasons=[cell["reason"]] if cell["reason"] else [],
            review_category=cell["review_category"],
        )
    counts = classification["counts"]["candidate_cells"]
    return {
        "generated_at": timestamp(),
        "items": items,
        "counts": counts,
        "spot_check": classification["spot_check"],
        "spot_check_ids": [
            f"{row['retailer']}:{row['catalog_id']}:{row['candidate_id']}"
            for row in classification["spot_check"]
        ],
    }


def sprint_audit(app: SprintApp, *, limit: int = 50) -> dict[str, Any]:
    """Recent decision trail: SQLite transitions plus decision diagnostics."""
    store = app.store()
    with closing(store.connection()) as connection:
        connection.row_factory = sqlite3.Row
        transitions = [
            dict(row)
            for row in connection.execute(
                "SELECT changed_at, retailer, catalog_id, from_state, to_state, "
                "category, candidate_id, reason, changed_by "
                "FROM discovery_state_transitions "
                "ORDER BY changed_at DESC, rowid DESC LIMIT ?",
                (limit,),
            )
        ]
        diagnostics = [
            dict(row)
            for row in connection.execute(
                "SELECT created_at, retailer, catalog_id, level, event, message "
                "FROM discovery_diagnostics "
                "ORDER BY created_at DESC, rowid DESC LIMIT ?",
                (limit,),
            )
        ]
    return {"transitions": transitions, "diagnostics": diagnostics}


def _validate_retailer(retailer: Any) -> str:
    if retailer not in read.RETAILER_SLUGS:
        raise ValueError(f"unsupported retailer: {retailer!r}")
    return str(retailer)


def _require(payload: dict[str, Any], key: str) -> str:
    value = payload.get(key)
    if not isinstance(value, str) or not value:
        raise ValueError(f"missing required field: {key}")
    return value


def sprint_decide(app: SprintApp, payload: dict[str, Any]) -> dict[str, Any]:
    """Apply one Review Decision through the real CLI seam."""
    action = payload.get("action")
    if action not in _SPRINT_ACTIONS:
        raise ValueError(f"unsupported action: {action!r}")
    retailer = _validate_retailer(payload.get("retailer"))
    catalog_id = _require(payload, "catalog_id")
    reason = payload.get("reason")
    if reason is not None and not isinstance(reason, str):
        raise ValueError("reason must be a string")
    reason = reason or None
    store = app.store()
    if action == "approve":
        try:
            result = approve(
                store, retailer=retailer, catalog_id=catalog_id,
                candidate_id=_require(payload, "candidate_id"),
                mapping_path=app.mappings_path, rejection_path=app.rejections_path,
                decided_by=app.decided_by, reason=reason,
            )
        except ValueError as exc:
            # Sprint intent for `a` on an already-approved cell: this candidate
            # becomes the mapping. Same-candidate approve is idempotent (no
            # error), so this is always a different candidate → replace.
            if "already has an approved mapping" not in str(exc):
                raise
            result = replace_mapping(
                store, retailer=retailer, catalog_id=catalog_id,
                candidate_id=_require(payload, "candidate_id"),
                mapping_path=app.mappings_path, decided_by=app.decided_by,
                reason=reason or "sprint: approve replaced previous mapping",
            )
            result = {**result, "replaced": True}
    elif action == "reject":
        result = reject_listing(
            store, retailer=retailer, catalog_id=catalog_id,
            candidate_id=_require(payload, "candidate_id"),
            mapping_path=app.mappings_path, rejection_path=app.rejections_path,
            decided_by=app.decided_by, reason=reason,
        )
    elif action == "exclude":
        result = do_not_map_cell(
            store, retailer=retailer, catalog_id=catalog_id,
            rejection_path=app.rejections_path, decided_by=app.decided_by,
            reason=reason,
        )
    elif action == "replace":
        result = replace_mapping(
            store, retailer=retailer, catalog_id=catalog_id,
            candidate_id=_require(payload, "candidate_id"),
            mapping_path=app.mappings_path, decided_by=app.decided_by,
            reason=_require(payload, "reason"),
        )
    else:  # challenge: keep or replace the approved mapping
        resolve_challenge(
            store, retailer=retailer, catalog_id=catalog_id,
            action=payload.get("challenge_action") or "keep",
            decided_by=app.decided_by, mapping_path=app.mappings_path,
            reason=reason,
        )
        result = {"status": payload.get("challenge_action") or "keep"}
    store.diagnostic(
        event="sprint_decision", retailer=retailer, catalog_id=catalog_id,
        details={
            "action": action, "decided_by": app.decided_by,
            "candidate_id": payload.get("candidate_id"), "reason": reason,
        },
    )
    return {"action": action, "result": result}


def sprint_batch(app: SprintApp, payload: dict[str, Any]) -> dict[str, Any]:
    """Apply one action to many cells; every item goes through the real seam."""
    action = payload.get("action")
    if action not in _BATCH_ACTIONS:
        raise ValueError(f"unsupported batch action: {action!r}")
    items = payload.get("items")
    if not isinstance(items, list) or not items:
        raise ValueError("items must be a non-empty list")
    reason = payload.get("reason") or None
    if reason is not None and not isinstance(reason, str):
        raise ValueError("reason must be a string")
    results: list[dict[str, Any]] = []
    for item in items:
        if not isinstance(item, dict):
            raise ValueError("batch items must be objects")
        entry: dict[str, Any] = {
            "retailer": item.get("retailer"),
            "catalog_id": item.get("catalog_id"),
            "candidate_id": item.get("candidate_id"),
        }
        try:
            outcome = sprint_decide(app, {**item, "action": action, "reason": reason})
            entry.update({"status": "ok", "result": outcome["result"]})
        except (ValueError, OSError) as exc:
            entry.update({"status": "skipped", "error": str(exc)})
        results.append(entry)
    applied = sum(1 for entry in results if entry["status"] == "ok")
    app.store().diagnostic(
        event="sprint_batch",
        details={
            "action": action, "decided_by": app.decided_by,
            "items": len(items), "applied": applied, "skipped": len(results) - applied,
        },
    )
    return {"action": action, "applied": applied, "skipped": len(results) - applied,
            "results": results}


def handle_request(
    app: SprintApp,
    method: str,
    path: str,
    query: dict[str, list[str]],
    body: bytes = b"",
) -> tuple[int, bytes, str]:
    """Route one HTTP request. Returns (status, body, content_type)."""
    if path == "/" or path == "/index.html":
        if method != "GET":
            return _json_bytes({"error": "method not allowed"}, status=405)
        return _html_bytes(_render_shell(app))

    payload: dict[str, Any] = {}
    if method == "POST":
        try:
            parsed = json.loads(body.decode("utf-8")) if body else {}
        except (ValueError, UnicodeDecodeError):
            return _json_bytes({"error": "invalid JSON body"}, status=400)
        if not isinstance(parsed, dict):
            return _json_bytes({"error": "body must be a JSON object"}, status=400)
        payload = parsed
    elif method != "GET":
        return _json_bytes({"error": "method not allowed"}, status=405)

    try:
        if path == "/api/sprint/progress":
            return _json_bytes(read.sprint_progress(app.load()))
        if path == "/api/sprint/queue":
            return _json_bytes(sprint_queue(app))
        if path == "/api/sprint/audit":
            return _json_bytes(sprint_audit(app))
        if path == "/api/sprint/decide":
            if method != "POST":
                return _json_bytes({"error": "method not allowed"}, status=405)
            return _json_bytes(sprint_decide(app, payload))
        if path == "/api/sprint/batch":
            if method != "POST":
                return _json_bytes({"error": "method not allowed"}, status=405)
            return _json_bytes(sprint_batch(app, payload))
    except ValueError as exc:
        return _json_bytes({"error": str(exc)}, status=400)
    except Exception as exc:  # pragma: no cover - defensive
        traceback.print_exc()
        return _json_bytes({"error": f"internal error: {exc}"}, status=500)
    return _json_bytes({"error": "not found"}, status=404)


def make_handler(app: SprintApp) -> type[BaseHTTPRequestHandler]:
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, format: str, *args: Any) -> None:  # noqa: A003
            sys.stderr.write("%s - %s\n" % (self.address_string(), format % args))

        def _dispatch(self) -> None:
            parsed = urlparse(self.path)
            query = parse_qs(parsed.query)
            length = int(self.headers.get("Content-Length") or 0)
            body = self.rfile.read(length) if length else b""
            try:
                status, out, content_type = handle_request(
                    app, self.command, parsed.path, query, body
                )
            except Exception as exc:  # pragma: no cover - defensive
                traceback.print_exc()
                status, out, content_type = _json_bytes(
                    {"error": f"internal error: {exc}"}, status=500
                )
            self.send_response(status)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(out)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            if self.command != "HEAD":
                self.wfile.write(out)

        def do_GET(self) -> None:  # noqa: N802
            self._dispatch()

        def do_HEAD(self) -> None:  # noqa: N802
            self._dispatch()

        def do_POST(self) -> None:  # noqa: N802
            self._dispatch()

    return Handler


def _render_shell(app: SprintApp) -> str:
    boot = {
        "decided_by": app.decided_by,
        "database": str(app.database_path),
        "catalog_packs": len(app.catalog),
    }
    boot_json = json.dumps(boot, default=str).replace("</", "<\\/")
    return f"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Pourpoint — Review Sprint</title>
<style>
{DASHBOARD_CSS}
{SPRINT_CSS}
</style>
</head>
<body>
<div id="app" class="console">
  <aside class="sidebar" aria-label="Primary">
    <div class="brand"><span class="brand-mark">P</span>pourpoint</div>
    <div class="side-label">Sprint</div>
    <nav class="side-nav">
      <button type="button" class="active" id="nav-queue"><span>⌁</span>Review queue</button>
    </nav>
    <div class="side-ref">
      <div class="side-label">Keys</div>
      <div class="ref-keys">
        <div class="ref-row"><span class="keycap">j/k</span> focus</div>
        <div class="ref-row"><span class="keycap">a</span> approve</div>
        <div class="ref-row"><span class="keycap">r</span> reject</div>
        <div class="ref-row"><span class="keycap">x</span> exclude</div>
        <div class="ref-row"><span class="keycap">s</span> select for batch</div>
        <div class="ref-row"><span class="keycap">A/R/X</span> apply to selection</div>
        <div class="ref-row"><span class="keycap">p</span> refresh</div>
      </div>
      <div class="side-label">When to press what</div>
      <div class="ref-rules">
        <p><span class="keycap">a</span> every attribute matches — same brand, product, flavour/variant, pack count, bottle size, package type. This listing <em>is</em> the pack.</p>
        <p><span class="keycap">r</span> the listing is a different product — Coke Cherry or No Caffeine vs Diet Coke, wrong size, wrong pack. The cell stays open for other candidates.</p>
        <p><span class="keycap">x</span> this retailer doesn't sell this pack at all — nothing should ever map here.</p>
        <p class="ref-note">No candidates at all? Just move on — no evidence yet isn't "not stocked". Reserve <span class="keycap">x</span> for packs the retailer genuinely doesn't sell.</p>
      </div>
    </div>
  </aside>
  <main class="console-main">
    <header class="console-header">
      <h1>Review sprint</h1>
      <span class="pill"><span class="dot" style="color:#e6ac58"></span>Decisions write <span class="mono" id="decided-by"></span></span>
    </header>
    <div class="console-body">
      <div class="status-banner" id="progress-banner"><span>Loading progress…</span></div>
    <div class="status-banner" id="error-banner" style="display:none"></div>
      <div class="sprint-grid">
        <section class="card sprint-queue" id="queue">
          <div class="card-head"><div><h2>Queue</h2><div class="muted" style="margin-top:5px;font-size:12px">Class A batch first, then per-item</div></div>
          <span class="mono muted" id="queue-count"></span></div>
          <div id="queue-list" class="queue-list" aria-live="polite">Loading…</div>
        </section>
        <section class="card sprint-compare" id="compare">
          <div class="card-head"><div><h2>Side-by-side</h2><div class="muted" style="margin-top:5px;font-size:12px">Candidate evidence vs catalog pack attributes</div></div></div>
          <div id="compare-body" class="compare-body">Select an item (j/k).</div>
        </section>
      </div>
      <section class="card" style="margin-top:14px">
        <div class="card-head"><div><h2>Audit trail</h2><div class="muted" style="margin-top:5px;font-size:12px">Recent decision transitions (from the real seam)</div></div>
        <button type="button" class="small-link" id="audit-toggle">Show →</button></div>
        <div id="audit-body" class="table-wrap" style="display:none"><table class="table"><thead><tr><th>When</th><th>Cell</th><th>From → To</th><th>By</th><th>Reason</th></tr></thead><tbody id="audit-rows"></tbody></table></div>
      </section>
    </div>
  </main>
</div>
<script id="boot" type="application/json">{boot_json}</script>
<script>
{SPRINT_JS}
</script>
</body>
</html>
"""


SPRINT_CSS = """
.sidebar { display:flex; flex-direction:column; overflow-y:auto; }
.side-ref { margin-top:auto; border-top:1px solid #426256; padding-top:6px; display:flex; flex-direction:column; flex:1 1 auto; min-height:0; }
.ref-keys { display:grid; gap:1px; padding:0 11px 8px; }
.ref-row { display:flex; align-items:center; gap:9px; padding:2px 0; font-size:12.5px; color:#b5c9bd; }
.keycap { display:inline-grid; place-items:center; min-width:22px; height:20px; padding:0 6px; border:1px solid #426256; border-radius:6px; background:#2b5145; color:#fff; font:11px 'DM Mono',monospace; letter-spacing:.02em; flex:none; }
.ref-rules { padding:0 11px 12px; overflow-y:auto; min-height:0; }
.ref-rules p { margin:0; padding:8px 0; border-bottom:1px solid #24483c; font-size:12.5px; line-height:1.55; color:#b5c9bd; }
.ref-rules::-webkit-scrollbar { width:8px; }
.ref-rules::-webkit-scrollbar-thumb { background:#2b5145; border-radius:4px; }
.ref-rules::-webkit-scrollbar-track { background:transparent; }
.ref-rules p:last-child { border-bottom:0; }
.ref-rules .keycap { margin-right:2px; }
.ref-rules em { color:#fff; font-style:normal; font-weight:600; }
.ref-note { color:#88a296; }
.ref-note .keycap { background:transparent; color:#88a296; }
.sprint-grid { display:grid; grid-template-columns:minmax(320px,5fr) minmax(420px,7fr); gap:14px; align-items:start; }
.queue-list { display:grid; gap:4px; padding:10px; max-height:62vh; overflow:auto; }
.queue-item { padding:10px 12px; border-radius:10px; border:1px solid transparent; cursor:pointer; display:grid; grid-template-columns:auto 1fr auto; gap:10px; align-items:start; }
.queue-item:hover { background:#f3f8f4; }
.queue-item.focused { border-color:var(--green); background:#eef7f0; }
.queue-item.selected { border-color:var(--blue); background:#eef3fa; }
.queue-item.done { opacity:.45; }
.qclass { display:inline-grid; place-items:center; min-width:22px; height:22px; border-radius:7px; font-weight:700; font-size:12px; }
.qclass.A { background:#e3f6ea; color:#1b6b45; }
.qclass.B { background:#fff1df; color:#8a5a1b; }
.qclass.C { background:#e8eef8; color:#355a8a; }
.qclass.D { background:#f1f4f2; color:#8a9691; }
.qclass.ch { background:#fde8e4; color:#8a3228; }
.qname { font-weight:600; }
.qmeta { font-size:12px; color:var(--muted); margin-top:2px; }
.qprice { font:600 14px 'Space Grotesk',sans-serif; }
.compare-body { padding:16px 18px 22px; }
.cmp-head h3 { margin:0 0 2px; font:600 18px 'Space Grotesk',sans-serif; }
.cmp-actions { display:flex; gap:8px; margin:14px 0 4px; flex-wrap:wrap; }
.cmp-actions button { border:1px solid var(--line); background:#fff; border-radius:9px; padding:8px 14px; font-weight:600; }
.cmp-actions button.approve { background:#e3f6ea; border-color:#bfe3cd; color:#1b6b45; }
.cmp-actions button.reject { background:#fde8e4; border-color:#f3cfc7; color:#8a3228; }
.cmp-actions button.exclude { background:#f1f4f2; color:#5a6862; }
.cmp-actions button:hover { filter:brightness(.97); }
table.cmp { width:100%; border-collapse:collapse; margin-top:12px; }
table.cmp th, table.cmp td { border-top:1px solid var(--line); padding:8px 10px; text-align:left; font-size:13px; }
table.cmp th { font-size:11px; text-transform:uppercase; color:var(--muted); }
td.ok { color:#1b6b45; font-weight:600; }
td.bad { color:#8a3228; font-weight:600; }
.evidence { margin-top:12px; padding:12px 14px; background:#f7f9f5; border:1px solid var(--line); border-radius:10px; font-size:13px; display:grid; gap:4px; }
.progress-bar { display:flex; height:14px; border-radius:8px; overflow:hidden; background:#f1f4f2; margin-top:8px; }
.progress-bar span { display:block; height:100%; }
.pb-observed { background:#116b55; } .pb-mapped { background:#7f9b8e; }
.pb-excluded { background:#c7604f; } .pb-review { background:#f2a65a; }
.pb-untouched { background:#dfe7e2; }
.progress-legend { display:flex; flex-wrap:wrap; gap:14px; font-size:12px; color:var(--muted); margin-top:8px; }
.progress-legend b { color:var(--ink); }
@media (max-width:1100px) { .sprint-grid { grid-template-columns:1fr; } }
"""


SPRINT_JS = r"""
const boot = JSON.parse(document.getElementById('boot').textContent);
let queue = null;
let focus = 0;
let selected = new Set();
let done = new Set();
let auditVisible = false;

function esc(v){ return String(v ?? '').replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c])); }
function key(item){ return `${item.retailer}:${item.catalog_id}:${item.candidate_id || ''}`; }
function money(v){ if (v == null || v === '') return '—'; const n = Number(v); return Number.isNaN(n) ? esc(v) : '€' + n.toFixed(2); }

async function api(path, opts) {
  const res = await fetch(path, opts || {});
  const data = await res.json();
  if (!res.ok) throw new Error(data.error || res.statusText);
  return data;
}
function post(path, payload){ return api(path, {method:'POST', headers:{'Content-Type':'application/json'}, body: JSON.stringify(payload)}); }
let errorTimer = null;
function showNote(msg, isError) {
  const el = document.getElementById('error-banner');
  el.textContent = msg;
  el.style.display = 'block';
  el.style.color = isError ? '#ff8a8a' : '#9fe0b0';
  clearTimeout(errorTimer);
  errorTimer = setTimeout(() => { el.style.display = 'none'; }, isError ? 8000 : 4000);
}

async function refresh() {
  const [q, p] = await Promise.all([api('/api/sprint/queue'), api('/api/sprint/progress')]);
  queue = q;
  renderProgress(p);
  renderQueue();
  loadAudit();
}

function renderProgress(p) {
  const b = p.buckets;
  const t = p.total_cells || 1;
  const pct = n => (100 * n / t).toFixed(1) + '%';
  document.getElementById('progress-banner').innerHTML = `
    <span><strong>${p.catalog_packs} packs × ${p.retailer_count} retailers = ${p.total_cells} cells.</strong>
      Observed <b>${b.observed}</b> · mapped-not-observed <b>${b.mapped_not_observed}</b> ·
      excluded <b>${b.excluded}</b> · in review <b>${b.in_review}</b> · untouched <b>${b.untouched}</b>
      <div class="progress-bar">
        <span class="pb-observed" style="width:${pct(b.observed)}"></span>
        <span class="pb-mapped" style="width:${pct(b.mapped_not_observed)}"></span>
        <span class="pb-excluded" style="width:${pct(b.excluded)}"></span>
        <span class="pb-review" style="width:${pct(b.in_review)}"></span>
        <span class="pb-untouched" style="width:${pct(b.untouched)}"></span>
      </div>
      <div class="progress-legend">
        <span><span class="dot" style="color:#116b55"></span>observed</span>
        <span><span class="dot" style="color:#7f9b8e"></span>mapped-not-observed</span>
        <span><span class="dot" style="color:#c7604f"></span>excluded</span>
        <span><span class="dot" style="color:#f2a65a"></span>in review</span>
        <span><span class="dot" style="color:#b9c6bf"></span>untouched</span>
        <span class="mono">strict bar: 100 packs × all retailers</span>
      </div></span>
    <span class="pill good"><span class="dot"></span>${b.observed}/${t} observed</span>`;
}

function renderQueue() {
  const list = document.getElementById('queue-list');
  if (!queue.items.length) {
    list.innerHTML = '<div class="empty"><strong>Queue empty</strong>Every cell is decided. Run a discovery/classification pass for more evidence.</div>';
    document.getElementById('queue-count').textContent = '0 items';
    return;
  }
  document.getElementById('queue-count').textContent = queue.items.length + ' items · ' + queue.counts.A + ' class A';
  focus = Math.min(focus, queue.items.length - 1);
  list.innerHTML = queue.items.map((item, i) => {
    const k = key(item);
    const cls = item.review_category === 'challenge' ? 'ch' : (item.class || '?');
    const spot = queue.spot_check_ids.includes(k) ? ' <span class="pill">spot-check</span>' : '';
    return `<div class="queue-item ${i === focus ? 'focused' : ''} ${selected.has(k) ? 'selected' : ''} ${done.has(k) ? 'done' : ''}" data-i="${i}">
      <span class="qclass ${cls}">${cls}</span>
      <div><div class="qname">${esc(item.evidence.name || item.pack_name)}</div>
      <div class="qmeta">${esc(item.retailer_name)} → ${esc(item.catalog_id)}${item.reasons.length ? ' · ' + esc(item.reasons.join('; ')) : ''}${spot}</div></div>
      <span class="qprice">${money(item.evidence.price)}</span></div>`;
  }).join('');
  list.querySelectorAll('.queue-item').forEach(el => {
    el.addEventListener('click', () => { focus = Number(el.dataset.i); renderQueue(); });
    el.addEventListener('dblclick', () => { focus = Number(el.dataset.i); decide('approve'); });
  });
  list.querySelector('.queue-item.focused')?.scrollIntoView({ block: 'nearest' });
  renderCompare();
}

function renderCompare() {
  const body = document.getElementById('compare-body');
  if (!queue.items.length) { body.innerHTML = '<div class="muted">Nothing to compare.</div>'; return; }
  const item = queue.items[focus];
  const rows = item.comparison.map(r => `
    <tr><th>${esc(r.label)}</th><td class="${r.match ? 'ok' : 'bad'}">${r.match ? '✓' : '✗'} ${esc(r.candidate ?? '—')}</td><td>${esc(r.pack ?? '—')}</td></tr>`).join('');
  const ev = item.evidence;
  const spot = queue.spot_check_ids.includes(key(item));
  body.innerHTML = `
    <div class="cmp-head">
      <h3>${esc(ev.name || '(no name)')}</h3>
      <div class="mono muted">${esc(item.retailer_name)} → ${esc(item.pack_name)} (${esc(item.catalog_id)}) · class ${esc(item.class || 'review')}${spot ? ' · spot-check sample' : ''}</div>
    </div>
    <div class="cmp-actions">
      <button type="button" class="approve" data-act="approve">Approve (a)</button>
      <button type="button" class="reject" data-act="reject">Reject listing (r)</button>
      <button type="button" class="exclude" data-act="exclude">Exclude cell (x)</button>
    </div>
    <table class="cmp"><thead><tr><th>Attribute</th><th>Candidate</th><th>Catalog pack</th></tr></thead><tbody>${rows}</tbody></table>
    <div class="evidence">
      <div><strong>Price:</strong> ${money(ev.price)} ${ev.price_status && ev.price_status !== 'valid' ? `<span class="pill warn">parse: ${esc(ev.price_status)}</span>` : ''}</div>
      <div><strong>Identity tier:</strong> ${esc(ev.identity_tier || '—')} · <strong>Source ref:</strong> ${esc(ev.source_product_reference || ev.source_item_id || '—')}</div>
      ${Object.keys(ev.diffs || {}).length ? `<div><strong>Attribute diffs:</strong> <span class="mono">${esc(JSON.stringify(ev.diffs))}</span></div>` : ''}
      ${item.reasons.length ? `<div><strong>Why in queue:</strong> ${esc(item.reasons.join('; '))}</div>` : ''}
      ${item.cell_reason ? `<div class="muted">${esc(item.cell_reason)}</div>` : ''}
    </div>`;
  body.querySelectorAll('[data-act]').forEach(btn => btn.addEventListener('click', () => decide(btn.dataset.act)));
}

async function decide(action) {
  if (!queue.items.length) return;
  const item = queue.items[focus];
  try {
    const payload = {action, retailer: item.retailer, catalog_id: item.catalog_id};
    if (item.candidate_id && action !== 'exclude') payload.candidate_id = item.candidate_id;
    if (item.review_category === 'challenge' && action === 'approve') payload.action = 'challenge', payload.challenge_action = 'replace', payload.reason = 'sprint: replaced via challenge resolution';
    const res = await post('/api/sprint/decide', payload);
    if (res.result && res.result.replaced) showNote('Approved — replaced the cell\'s previous mapping', false);
    done.add(key(item));
    focus = Math.min(focus + 1, queue.items.length - 1);
    await refresh();
  } catch (err) { showNote(err.message || String(err), true); }
}

async function batch(action) {
  if (!selected.size) return;
  const items = queue.items.filter(i => selected.has(key(i)) && !done.has(key(i)))
    .map(i => ({retailer: i.retailer, catalog_id: i.catalog_id, candidate_id: i.candidate_id}));
  if (!items.length) return;
  try {
    const res = await post('/api/sprint/batch', {action, items});
    items.forEach(i => done.add(key(i)));
    selected.clear();
    await refresh();
    showNote(`Batch ${action}: ${res.applied} applied, ${res.skipped} skipped`, res.skipped > 0);
  } catch (err) { showNote(err.message || String(err), true); }
}

async function loadAudit() {
  const data = await api('/api/sprint/audit');
  document.getElementById('audit-rows').innerHTML = data.transitions.map(t => `
    <tr><td class="mono muted">${esc(t.changed_at)}</td>
    <td>${esc(t.retailer)} / ${esc(t.catalog_id)}</td>
    <td><span class="pill">${esc(t.from_state || '∅')} → ${esc(t.to_state)}</span></td>
    <td class="mono muted">${esc(t.changed_by || '')}</td>
    <td class="muted">${esc(t.reason || '')}</td></tr>`).join('')
    || '<tr><td colspan="5" class="muted">No decisions recorded yet</td></tr>';
}

document.getElementById('nav-queue').addEventListener('click', () => refresh());
document.getElementById('audit-toggle').addEventListener('click', () => {
  auditVisible = !auditVisible;
  document.getElementById('audit-body').style.display = auditVisible ? 'block' : 'none';
  document.getElementById('audit-toggle').textContent = auditVisible ? 'Hide' : 'Show →';
});

addEventListener('keydown', ev => {
  if (ev.target.tagName === 'INPUT' || ev.target.tagName === 'TEXTAREA') return;
  if (!queue) return;
  const n = queue.items.length;
  if (ev.key === 'j' || ev.key === 'ArrowDown') { focus = Math.min(focus + 1, n - 1); ev.preventDefault(); renderQueue(); }
  else if (ev.key === 'k' || ev.key === 'ArrowUp') { focus = Math.max(focus - 1, 0); ev.preventDefault(); renderQueue(); }
  else if (ev.key === 'a') decide('approve');
  else if (ev.key === 'r') decide('reject');
  else if (ev.key === 'x') decide('exclude');
  else if (ev.key === 's' && n) { const k = key(queue.items[focus]); selected.has(k) ? selected.delete(k) : selected.add(k); renderQueue(); }
  else if (ev.key === 'A') batch('approve');
  else if (ev.key === 'R') batch('reject');
  else if (ev.key === 'X') batch('exclude');
  else if (ev.key === 'p') refresh();
});

document.getElementById('decided-by').textContent = boot.decided_by;
refresh();
"""


def _port_available(host: str, port: int) -> None:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            sock.bind((host, port))
        except OSError as exc:
            raise SystemExit(f"cannot bind sprint dashboard to {host}:{port}: {exc}") from exc


def create_server(app: SprintApp, *, host: str, port: int) -> ThreadingHTTPServer:
    return ThreadingHTTPServer((host, port), make_handler(app))


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Review-sprint prototype: keyboard-driven mapping review (ticket 05)"
    )
    parser.add_argument("--repo-root", type=Path, default=None)
    parser.add_argument(
        "--database", type=Path, default=None,
        help="discovery SQLite store (default: DRINKS_DATABASE or data/feed.sqlite)",
    )
    parser.add_argument("--catalog", type=Path, default=None)
    parser.add_argument("--mappings", type=Path, default=None)
    parser.add_argument("--rejections", type=Path, default=None)
    parser.add_argument("--decided-by", default="sprint-operator")
    parser.add_argument("--host", default=DEFAULT_HOST)
    parser.add_argument(
        "--port", type=int,
        default=int(os.environ.get("DRINKS_SPRINT_PORT", DEFAULT_PORT)),
    )
    parser.add_argument("--no-browser", action="store_true")
    args = parser.parse_args(argv)

    try:
        repo_root = args.repo_root.resolve() if args.repo_root else read.resolve_repo_root()
    except FileNotFoundError as exc:
        print(str(exc), file=sys.stderr)
        return 2

    database_path = (
        args.database.resolve() if args.database else read.default_database_path(repo_root)
    )
    if not database_path.exists():
        print(
            f"discovery database not found: {database_path}\n"
            "point --database at a discovery store (the sprint writes decisions there)",
            file=sys.stderr,
        )
        return 2

    if args.host not in {"127.0.0.1", "localhost", "::1"}:
        print(f"refusing non-loopback host {args.host!r}", file=sys.stderr)
        return 2

    try:
        app = SprintApp(
            repo_root,
            database_path=database_path,
            catalog_path=args.catalog,
            mappings_path=args.mappings,
            rejections_path=args.rejections,
            decided_by=args.decided_by,
        )
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        print(f"cannot initialize sprint workspace: {exc}", file=sys.stderr)
        return 2

    _port_available(args.host, args.port)
    server = create_server(app, host=args.host, port=args.port)
    url = f"http://{args.host}:{args.port}/"
    print(f"Review Sprint prototype {url}")
    print(f"  catalog     {app.catalog_path} ({len(app.catalog)} packs)")
    print(f"  database    {app.database_path}")
    print(f"  mappings    {app.mappings_path}")
    print(f"  rejections  {app.rejections_path}")
    print(f"  decided_by  {app.decided_by}")
    print("  prototype   decisions write via discovery_cli seam · Ctrl+C to stop")

    if not args.no_browser:
        threading.Timer(0.8, lambda: webbrowser.open(url)).start()

    def _shutdown(signum: int, frame: Any) -> None:  # noqa: ARG001
        threading.Thread(target=server.shutdown, daemon=True).start()

    signal.signal(signal.SIGINT, _shutdown)
    signal.signal(signal.SIGTERM, _shutdown)
    try:
        server.serve_forever(poll_interval=0.5)
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
