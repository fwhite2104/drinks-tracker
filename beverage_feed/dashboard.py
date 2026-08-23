"""Local read-only Operator Dashboard HTTP app and launcher.

Binds to 127.0.0.1 only (default port 8765), serves an operator-console shell
over the ``dashboard_read`` seam, and optionally opens the system browser.
Never creates or migrates SQLite; empty/partial/stale states stay truthful.
"""

from __future__ import annotations

import argparse
import html
import json
import os
import signal
import socket
import sys
import threading
import traceback
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlparse

from . import dashboard_read as read
from .collector import load_catalog

DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 8765


class DashboardApp:
    """In-process dashboard: path resolution + request handlers."""

    def __init__(
        self,
        repo_root: Path,
        *,
        database_path: Path | None = None,
        catalog_path: Path | None = None,
        mappings_path: Path | None = None,
        rejections_path: Path | None = None,
    ) -> None:
        self.repo_root = repo_root.resolve()
        self.database_path = database_path
        self.catalog_path = catalog_path
        self.mappings_path = mappings_path
        self.rejections_path = rejections_path

    def load(self) -> read.WorkspaceSnapshot:
        return read.load_workspace(
            self.repo_root,
            database_path=self.database_path,
            catalog_path=self.catalog_path,
            mappings_path=self.mappings_path,
            rejections_path=self.rejections_path,
        )


def _json_bytes(payload: Any, *, status: int = 200) -> tuple[int, bytes, str]:
    body = json.dumps(payload, default=str, indent=2).encode("utf-8")
    return status, body, "application/json; charset=utf-8"


def _html_bytes(document: str, *, status: int = 200) -> tuple[int, bytes, str]:
    return status, document.encode("utf-8"), "text/html; charset=utf-8"


def handle_request(
    app: DashboardApp,
    method: str,
    path: str,
    query: dict[str, list[str]],
) -> tuple[int, bytes, str]:
    """Route one HTTP request. Returns (status, body, content_type)."""
    if method not in {"GET", "HEAD"}:
        return _json_bytes({"error": "method not allowed"}, status=405)

    try:
        snapshot = app.load()
    except FileNotFoundError as exc:
        return _json_bytes({"error": str(exc)}, status=500)
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        return _json_bytes({"error": f"cannot load workspace: {exc}"}, status=500)

    if path in {"/", "/index.html"}:
        return _html_bytes(_render_shell(snapshot))

    if path == "/api/overview":
        return _json_bytes(
            {
                "stats": read.overview_stats(snapshot),
                "collection_health": read.collection_health(snapshot),
                "discovery": read.discovery_summary(snapshot),
                "catalog": read.catalog_table(snapshot)[:25],
                "retailers": list(snapshot.retailers),
            }
        )

    if path == "/api/retailers":
        health = {row["retailer"]: row for row in read.collection_health(snapshot)}
        approved = read._approved_mapping_index(snapshot.mappings)
        counts: dict[str, int] = {r["slug"]: 0 for r in snapshot.retailers}
        for retailer, _catalog_id in approved:
            if retailer in counts:
                counts[retailer] += 1
        return _json_bytes(
            {
                "retailers": [
                    {
                        **row,
                        "approved_mappings": counts[row["slug"]],
                        "collection": health.get(row["slug"]),
                    }
                    for row in snapshot.retailers
                ]
            }
        )

    if path == "/api/catalog":
        return _json_bytes({"packs": read.catalog_table(snapshot)})

    if path == "/api/coverage":
        return _json_bytes(read.coverage_matrix(snapshot))

    if path == "/api/discovery":
        return _json_bytes(
            {
                "summary": read.discovery_summary(snapshot),
                "coverage": read.coverage_matrix(snapshot),
            }
        )

    if path == "/api/collection":
        return _json_bytes(
            {
                "health": read.collection_health(snapshot),
                "workspace_state": snapshot.workspace_state,
                "database": {
                    "path": str(snapshot.database.path),
                    "exists": snapshot.database.exists,
                    "openable": snapshot.database.openable,
                    "error": snapshot.database.error,
                },
            }
        )

    if path == "/api/feed":
        catalog_values = query.get("catalog_id") or []
        catalog_id = catalog_values[0] if catalog_values else None
        return _json_bytes(read.feed_preview(snapshot, catalog_id=catalog_id))

    if path.startswith("/api/pack/"):
        catalog_id = path[len("/api/pack/") :]
        if not catalog_id:
            return _json_bytes({"error": "catalog_id required"}, status=400)
        detail = read.pack_detail(snapshot, catalog_id)
        if detail is None:
            return _json_bytes({"error": "pack not found"}, status=404)
        return _json_bytes(detail)

    if path == "/api/workspace":
        return _json_bytes(
            {
                "repo_root": str(snapshot.repo_root),
                "catalog_path": str(snapshot.catalog_path),
                "mappings_path": str(snapshot.mappings_path),
                "rejections_path": str(snapshot.rejections_path),
                "workspace_state": snapshot.workspace_state,
                "database": {
                    "path": str(snapshot.database.path),
                    "exists": snapshot.database.exists,
                    "openable": snapshot.database.openable,
                    "error": snapshot.database.error,
                },
                "catalog_packs": len(snapshot.catalog),
                "retailers": list(snapshot.retailers),
            }
        )

    return _json_bytes({"error": "not found"}, status=404)


def make_handler(app: DashboardApp) -> type[BaseHTTPRequestHandler]:
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, format: str, *args: Any) -> None:  # noqa: A003
            sys.stderr.write("%s - %s\n" % (self.address_string(), format % args))

        def _dispatch(self) -> None:
            parsed = urlparse(self.path)
            query = parse_qs(parsed.query)
            try:
                status, body, content_type = handle_request(
                    app, self.command, parsed.path, query
                )
            except Exception as exc:  # pragma: no cover - defensive
                traceback.print_exc()
                status, body, content_type = _json_bytes(
                    {"error": f"internal error: {exc}"}, status=500
                )
            self.send_response(status)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            if self.command != "HEAD":
                self.wfile.write(body)

        def do_GET(self) -> None:  # noqa: N802
            self._dispatch()

        def do_HEAD(self) -> None:  # noqa: N802
            self._dispatch()

    return Handler


def _esc(value: Any) -> str:
    return html.escape("" if value is None else str(value), quote=True)


def _render_shell(snapshot: read.WorkspaceSnapshot) -> str:
    """Server-rendered operator-console shell; data filled client-side via /api/*."""
    stats = read.overview_stats(snapshot)
    db = snapshot.database
    state = snapshot.workspace_state
    if state == "no_database":
        banner = (
            f"<strong>Feed not initialized.</strong> No collection run or SQLite "
            f"database was found at <span class='mono'>{_esc(db.path)}</span>."
        )
        pill = "Not run yet"
        pill_class = "warn"
    elif not db.openable:
        banner = (
            f"<strong>Database unreadable.</strong> "
            f"{_esc(db.error or 'open failed')} "
            f"(<span class='mono'>{_esc(db.path)}</span>). "
            f"Catalog and mappings still load from JSON."
        )
        pill = "Database error"
        pill_class = "bad"
    elif state == "no_run":
        banner = (
            f"<strong>No collection run yet.</strong> Database exists at "
            f"<span class='mono'>{_esc(db.path)}</span> but has no runs."
        )
        pill = "No run yet"
        pill_class = "warn"
    else:
        banner = (
            f"<strong>Local workspace.</strong> Reading "
            f"<span class='mono'>{_esc(db.path)}</span> · "
            f"{stats['observation_count']} observations."
        )
        pill = "Partial feed" if stats["observation_count"] else "Collected"
        pill_class = "good"

    boot = {
        "workspace_state": state,
        "stats": stats,
        "retailers": list(snapshot.retailers),
    }
    boot_json = json.dumps(boot, default=str).replace("</", "<\\/")

    return f"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Pourpoint — Operator Dashboard</title>
<style>
{DASHBOARD_CSS}
</style>
</head>
<body>
<div id="app" class="console">
  <aside class="sidebar" aria-label="Primary">
    <div class="brand"><span class="brand-mark">P</span>pourpoint</div>
    <div class="side-label">Monitor</div>
    <nav class="side-nav" id="nav-monitor">
      <button type="button" data-view="overview" class="active"><span>◈</span>Overview</button>
      <button type="button" data-view="retailers"><span>◫</span>Retailers</button>
      <button type="button" data-view="catalog"><span>▦</span>Benchmark Catalog</button>
      <button type="button" data-view="discovery"><span>⌁</span>Discovery &amp; mapping</button>
      <button type="button" data-view="collection"><span>◷</span>Collection health</button>
    </nav>
    <div class="side-label">Preview</div>
    <nav class="side-nav" id="nav-preview">
      <button type="button" data-view="feed"><span>↗</span>Consumer feed</button>
    </nav>
    <div class="side-foot">
      <span class="dot" style="color:#e6ac58"></span> Local · read-only<br>
      <span class="mono">catalog.json · live</span>
    </div>
  </aside>
  <main class="console-main">
    <header class="console-header">
      <h1 id="page-title">Overview</h1>
      <span class="pill"><span class="dot" style="color:#7f9b8e"></span>Read-only mode</span>
    </header>
    <div class="console-body">
      <div class="status-banner" id="status-banner">
        <span>{banner}</span>
        <span class="pill {pill_class}"><span class="dot"></span>{_esc(pill)}</span>
      </div>
      <div id="view" class="view" aria-live="polite">Loading…</div>
    </div>
  </main>
</div>
<script id="boot" type="application/json">{boot_json}</script>
<script>
{DASHBOARD_JS}
</script>
</body>
</html>
"""


DASHBOARD_CSS = """
@import url('https://fonts.googleapis.com/css2?family=DM+Mono:wght@400;500&family=DM+Sans:wght@400;500;600;700&family=Space+Grotesk:wght@500;600;700&display=swap');
:root {
  --ink:#17211f; --muted:#6e7d78; --line:#dfe7e2; --paper:#f7f9f5;
  --panel:#fff; --green:#116b55; --lime:#c7ec72; --orange:#f2a65a;
  --red:#c7604f; --blue:#557ba8; --shadow:0 14px 32px #18382c0d;
}
* { box-sizing:border-box; }
body { margin:0; background:var(--paper); color:var(--ink); font:14px 'DM Sans', system-ui, sans-serif; }
button, input, select { font:inherit; }
button { cursor:pointer; }
button:focus-visible, a:focus-visible, select:focus-visible { outline:3px solid var(--lime); outline-offset:3px; }
a { color:var(--green); text-decoration:none; }
a:hover { text-decoration:underline; }
.mono { font:12px 'DM Mono', ui-monospace, monospace; }
.muted { color:var(--muted); }
.eyebrow { color:var(--green); font:11px 'DM Mono', monospace; text-transform:uppercase; letter-spacing:.08em; }
.console { min-height:100vh; display:grid; grid-template-columns:236px 1fr; }
.sidebar { background:#18332c; color:#dce9df; padding:24px 15px; position:sticky; top:0; height:100vh; }
.sidebar .brand { color:#fff; padding:0 10px 28px; font:600 18px 'Space Grotesk', sans-serif; display:flex; gap:10px; align-items:center; }
.brand-mark { display:inline-grid; place-items:center; width:28px; height:28px; border-radius:8px; background:var(--lime); color:#18332c; font-weight:700; font-size:14px; }
.side-label { padding:16px 11px 7px; color:#88a296; font:10px 'DM Mono', monospace; text-transform:uppercase; letter-spacing:.12em; }
.side-nav { display:grid; gap:3px; }
.side-nav button { border:0; background:transparent; color:#b5c9bd; text-align:left; padding:10px 11px; border-radius:8px; width:100%; }
.side-nav button.active, .side-nav button:hover { color:#fff; background:#2b5145; }
.side-nav button span { display:inline-block; width:24px; color:#7eaf8e; }
.side-foot { position:absolute; bottom:20px; left:15px; right:15px; border-top:1px solid #426256; padding:15px 11px; color:#9bb4a6; font-size:12px; }
.console-main { min-width:0; }
.console-header { display:flex; justify-content:space-between; align-items:center; padding:22px 32px; border-bottom:1px solid var(--line); background:var(--panel); position:sticky; top:0; z-index:5; }
.console-header h1 { margin:0; font:600 22px 'Space Grotesk', sans-serif; }
.console-body { padding:28px 32px 80px; }
.status-banner { display:flex; justify-content:space-between; gap:16px; align-items:center; padding:14px 16px; border:1px solid var(--line); border-radius:12px; background:#eef5ef; margin-bottom:22px; }
.pill { display:inline-flex; align-items:center; gap:7px; padding:5px 10px; border-radius:999px; background:#e8efe9; color:#2d4a40; font-size:12px; white-space:nowrap; }
.pill.warn { background:#fff1df; color:#8a5a1b; }
.pill.bad { background:#fde8e4; color:#8a3228; }
.pill.good { background:#e3f6ea; color:#1b6b45; }
.dot { width:7px; height:7px; border-radius:50%; background:currentColor; display:inline-block; }
.card { background:var(--panel); border:1px solid var(--line); border-radius:14px; box-shadow:var(--shadow); }
.card-head { display:flex; justify-content:space-between; gap:12px; align-items:flex-start; padding:18px 18px 0; }
.card-head h2 { margin:0; font:600 16px 'Space Grotesk', sans-serif; }
.stat-grid { display:grid; grid-template-columns:repeat(4, minmax(0,1fr)); gap:14px; }
.stat { padding:18px; }
.stat .number { display:block; font:700 28px 'Space Grotesk', sans-serif; margin:8px 0 4px; }
.stat .label { color:var(--muted); font-size:12px; }
.two-col { display:grid; grid-template-columns:1fr 1fr; gap:14px; margin-top:18px; }
.console-stack { display:grid; gap:14px; margin-top:18px; }
.table-wrap { overflow:auto; }
table.table { width:100%; border-collapse:collapse; }
table.table th, table.table td { text-align:left; padding:11px 16px; border-top:1px solid var(--line); vertical-align:top; }
table.table th { font-size:11px; text-transform:uppercase; letter-spacing:.06em; color:var(--muted); font-weight:600; }
table.table tr { cursor:default; }
table.table tbody tr.clickable { cursor:pointer; }
table.table tbody tr.clickable:hover { background:#f3f8f4; }
.health-list { padding:8px 8px 14px; display:grid; gap:6px; }
.health-row { display:grid; grid-template-columns:1fr auto; gap:10px; align-items:center; padding:10px 12px; border-radius:10px; }
.health-row:hover { background:#f3f8f4; }
.empty { padding:28px 18px 32px; text-align:center; color:var(--muted); }
.empty strong { display:block; color:var(--ink); margin-bottom:6px; }
.empty-icon { font-size:22px; margin-bottom:8px; opacity:.7; }
.small-link { font-size:12px; color:var(--green); background:none; border:0; padding:0; }
.coverage-wrap { overflow:auto; padding:0 0 8px; }
.coverage-table { border-collapse:collapse; min-width:720px; width:100%; }
.coverage-table th, .coverage-table td { border-top:1px solid var(--line); padding:8px 10px; text-align:center; font-size:12px; }
.coverage-table th:first-child, .coverage-table td:first-child { text-align:left; position:sticky; left:0; background:var(--panel); min-width:220px; }
.cell { display:inline-grid; place-items:center; width:28px; height:28px; border-radius:8px; font-weight:600; }
.cell.yes { background:#e3f6ea; color:#1b6b45; }
.cell.no { background:#f1f4f2; color:#8a9691; }
.cell.pend { background:#fff1df; color:#8a5a1b; }
.cell.rej { background:#fde8e4; color:#8a3228; }
.cell.ch { background:#e8eef8; color:#355a8a; }
.legend { padding:12px 18px; border-top:1px solid var(--line); display:flex; flex-wrap:wrap; gap:14px; font-size:12px; color:var(--muted); }
.feed-grid { display:grid; gap:14px; margin-top:18px; }
.feed-card { padding:0; overflow:hidden; }
.feed-card-head { padding:16px 18px; border-bottom:1px solid var(--line); display:flex; justify-content:space-between; gap:12px; align-items:flex-start; }
.feed-card-head h3 { margin:0; font:600 15px 'Space Grotesk', sans-serif; }
.retailer-slots { display:grid; grid-template-columns:repeat(5, minmax(0,1fr)); gap:0; }
.slot { padding:14px 12px; border-right:1px solid var(--line); min-height:118px; }
.slot:last-child { border-right:0; }
.slot .rname { font-size:11px; color:var(--muted); text-transform:uppercase; letter-spacing:.04em; margin-bottom:8px; }
.slot .price { font:700 20px 'Space Grotesk', sans-serif; }
.slot .price.best { color:var(--green); }
.slot .sub { font-size:11px; color:var(--muted); margin-top:4px; }
.slot .state { font-size:13px; font-weight:600; }
.slot .state.awaiting { color:#8a5a1b; }
.slot .state.na { color:#8a9691; }
.slot .state.temp { color:#8a3228; }
.slot .state.last { color:#355a8a; }
.rule { margin-top:14px; font-size:12px; color:var(--muted); padding:10px 12px; border-left:3px solid var(--lime); background:#eef5ef; border-radius:0 8px 8px 0; }
.detail-grid { display:grid; grid-template-columns:repeat(2, minmax(0,1fr)); gap:12px; margin-top:14px; }
.detail-cell { padding:14px 16px; border:1px solid var(--line); border-radius:12px; background:var(--panel); }
.detail-cell h3 { margin:0 0 8px; font-size:14px; }
.back { margin-bottom:14px; }
@media (max-width:980px) {
  .console { grid-template-columns:1fr; }
  .sidebar { display:none; }
  .stat-grid, .two-col, .retailer-slots, .detail-grid { grid-template-columns:1fr 1fr; }
}
@media (max-width:640px) {
  .stat-grid, .two-col, .retailer-slots, .detail-grid { grid-template-columns:1fr; }
  .console-body, .console-header { padding-left:16px; padding-right:16px; }
}
"""


DASHBOARD_JS = r"""
const boot = JSON.parse(document.getElementById('boot').textContent);
const viewEl = document.getElementById('view');
const titleEl = document.getElementById('page-title');
const titles = {
  overview: 'Overview',
  retailers: 'Retailers',
  catalog: 'Benchmark Catalog',
  discovery: 'Discovery & mapping',
  collection: 'Collection health',
  feed: 'Consumer feed',
  pack: 'Pack detail',
};

function esc(value) {
  return String(value ?? '').replace(/[&<>"']/g, c => (
    {'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]
  ));
}

async function api(path) {
  const res = await fetch(path, { headers: { 'Accept': 'application/json' } });
  const data = await res.json();
  if (!res.ok) throw new Error(data.error || res.statusText);
  return data;
}

function money(value) {
  if (value == null || value === '') return null;
  const n = Number(value);
  if (Number.isNaN(n)) return `€${esc(value)}`;
  return `€${n.toFixed(2)}`;
}

function setNav(view) {
  document.querySelectorAll('.side-nav button').forEach(btn => {
    btn.classList.toggle('active', btn.dataset.view === view);
  });
  titleEl.textContent = titles[view] || view;
}

function go(view, params = {}) {
  const url = new URL(location.href);
  url.searchParams.set('view', view);
  Object.entries(params).forEach(([k, v]) => {
    if (v == null || v === '') url.searchParams.delete(k);
    else url.searchParams.set(k, v);
  });
  if (view !== 'pack') url.searchParams.delete('catalog_id');
  if (view !== 'feed') url.searchParams.delete('focus');
  history.pushState({}, '', url);
  render();
}

function route() {
  const q = new URLSearchParams(location.search);
  return {
    view: q.get('view') || 'overview',
    catalog_id: q.get('catalog_id') || null,
    focus: q.get('focus') || null,
  };
}

function healthRows(rows) {
  if (!rows || !rows.length) return `<div class="empty"><strong>No retailers</strong></div>`;
  return rows.map(r => {
    const detail = r.state === 'not_collected'
      ? 'Not collected'
      : `${r.observed || 0} observed · ${r.source_error || 0} errors · ${esc(r.finished_at || '')}`;
    return `<div class="health-row"><div><strong>${esc(r.display_name || r.retailer)}</strong><div class="muted" style="margin-top:3px;font-size:12px">${esc(detail)}</div></div><span class="pill ${r.state==='not_collected'?'warn':'good'}">${esc(r.label || r.state)}</span></div>`;
  }).join('');
}

function catalogRows(rows) {
  if (!rows.length) return `<tr><td colspan="3" class="muted">No catalog packs</td></tr>`;
  return rows.map(r => `
    <tr class="clickable" data-pack="${esc(r.catalog_id)}">
      <td><strong>${esc(r.name)}</strong><div class="mono muted">${esc(r.catalog_id)}</div></td>
      <td>${r.approved_count}/${r.retailer_count}</td>
      <td>${esc(r.feed_label)}</td>
    </tr>`).join('');
}

async function renderOverview() {
  const data = await api('/api/overview');
  const s = data.stats;
  const discoveryEmpty = data.discovery.state === 'no_discovery_run';
  viewEl.innerHTML = `
    <div class="console-intro" style="display:flex;justify-content:space-between;gap:16px;align-items:end;margin-bottom:8px">
      <div><div class="eyebrow">Operator dashboard / local workspace</div>
      <h2 style="margin:6px 0 0;font:600 26px 'Space Grotesk',sans-serif">Know what the feed knows.</h2></div>
      <span class="mono muted">Updated from repository files</span>
    </div>
    <div class="stat-grid">
      <div class="card stat"><span class="eyebrow">Catalog</span><span class="number">${s.catalog_packs}</span><span class="label">benchmark packs</span></div>
      <div class="card stat"><span class="eyebrow">Mappings</span><span class="number">${s.approved_mappings}</span><span class="label">approved across retailers</span></div>
      <div class="card stat"><span class="eyebrow">Retailers</span><span class="number">${s.supported_retailers}</span><span class="label">supported sources</span></div>
      <div class="card stat"><span class="eyebrow">Observations</span><span class="number">${s.observation_count}</span><span class="label">price records</span></div>
    </div>
    <div class="console-stack">
      <div class="two-col">
        <section class="card">
          <div class="card-head"><div><h2>Collection health</h2><div class="muted" style="margin-top:5px;font-size:12px">Latest run by source</div></div>
          <button type="button" class="small-link" data-go="collection">View details →</button></div>
          <div class="health-list">${healthRows(data.collection_health)}</div>
        </section>
        <section class="card">
          <div class="card-head"><div><h2>Discovery state</h2><div class="muted" style="margin-top:5px;font-size:12px">Mapping coverage</div></div>
          <button type="button" class="small-link" data-go="discovery">Open matrix →</button></div>
          ${discoveryEmpty
            ? `<div class="empty"><div class="empty-icon">⌁</div><strong>No discovery run yet</strong>Discovery evidence will appear here once a mapping run has been recorded.</div>`
            : `<div class="health-list">${(data.discovery.per_retailer||[]).map(r => {
                const t = (r.approved||0)+(r.review||0)+(r.unmapped||0)+(r.pending||0);
                return `<div class="health-row"><div><strong>${esc(r.display_name||r.retailer)}</strong><div class="muted" style="margin-top:3px;font-size:12px">${r.approved||0} approved · ${r.review||0} review · ${r.pending||0} pending</div></div><span class="pill">${t} cells</span></div>`;
              }).join('')}</div>`}
        </section>
      </div>
      <section class="card">
        <div class="card-head"><div><h2>Benchmark Catalog</h2><div class="muted" style="margin-top:5px;font-size:12px">Packs operators care about</div></div>
        <button type="button" class="small-link" data-go="catalog">Browse all →</button></div>
        <div class="table-wrap"><table class="table"><thead><tr><th>Pack</th><th>Retailer mappings</th><th>Feed state</th></tr></thead>
        <tbody>${catalogRows(data.catalog)}</tbody></table></div>
      </section>
    </div>`;
  viewEl.querySelectorAll('[data-go]').forEach(btn => btn.addEventListener('click', () => go(btn.dataset.go)));
  viewEl.querySelectorAll('tr[data-pack]').forEach(tr => tr.addEventListener('click', () => go('pack', { catalog_id: tr.dataset.pack })));
}

async function renderRetailers() {
  const data = await api('/api/retailers');
  viewEl.innerHTML = `
    <div class="eyebrow">Supported sources</div>
    <h2 style="margin:6px 0 16px;font:600 22px 'Space Grotesk',sans-serif">Tier 1 retailers</h2>
    <div class="table-wrap card"><table class="table"><thead><tr><th>Retailer</th><th>Tier</th><th>Approved mappings</th><th>Collection</th></tr></thead>
    <tbody>${data.retailers.map(r => {
      const c = r.collection || {};
      return `<tr><td><strong>${esc(r.display_name)}</strong><div class="mono muted">${esc(r.slug)}</div></td>
        <td>${r.tier}</td><td>${r.approved_mappings}</td>
        <td>${esc(c.label || 'Not collected')}${c.finished_at ? `<div class="mono muted">${esc(c.finished_at)}</div>` : ''}</td></tr>`;
    }).join('')}</tbody></table></div>`;
}

async function renderCatalog() {
  const data = await api('/api/catalog');
  viewEl.innerHTML = `
    <div class="eyebrow">Benchmark Catalog</div>
    <h2 style="margin:6px 0 16px;font:600 22px 'Space Grotesk',sans-serif">${data.packs.length} packs</h2>
    <div class="table-wrap card"><table class="table"><thead><tr><th>Pack</th><th>Brand</th><th>Size</th><th>Mappings</th><th>Feed</th></tr></thead>
    <tbody>${data.packs.map(r => `
      <tr class="clickable" data-pack="${esc(r.catalog_id)}">
        <td><strong>${esc(r.name)}</strong><div class="mono muted">${esc(r.catalog_id)}</div></td>
        <td>${esc(r.brand)}</td>
        <td class="mono">${r.pack_count}×${r.unit_size_ml}ml ${esc(r.package_type)}</td>
        <td>${r.approved_count}/${r.retailer_count}</td>
        <td>${esc(r.feed_label)}</td>
      </tr>`).join('')}</tbody></table></div>`;
  viewEl.querySelectorAll('tr[data-pack]').forEach(tr => tr.addEventListener('click', () => go('pack', { catalog_id: tr.dataset.pack })));
}

function matrixCell(state) {
  if (state === 'approved') return `<span class="cell yes" title="approved">✓</span>`;
  if (state === 'pending' || state === 'review') return `<span class="cell pend" title="${esc(state)}">?</span>`;
  if (state === 'challenge') return `<span class="cell ch" title="challenge">!</span>`;
  if (state === 'do_not_map' || state === 'rejected') return `<span class="cell rej" title="${esc(state)}">×</span>`;
  if (state === 'dormant') return `<span class="cell no" title="dormant">·</span>`;
  return `<span class="cell no" title="unmapped">—</span>`;
}

async function renderDiscovery() {
  const data = await api('/api/discovery');
  const cov = data.coverage;
  const summary = data.summary;
  const head = cov.retailers.map(r => `<th>${esc(r.display_name.replace(' Stores','').replace(' Ireland',''))}</th>`).join('');
  const body = cov.packs.map(p => {
    const cells = cov.retailers.map(r => {
      const cell = p.cells[r.slug] || {};
      return `<td>${matrixCell(cell.mapping_state || 'unmapped')}</td>`;
    }).join('');
    return `<tr class="clickable" data-pack="${esc(p.catalog_id)}"><td><strong>${esc(p.name)}</strong><div class="mono muted">${esc(p.catalog_id)}</div></td>${cells}</tr>`;
  }).join('');
  viewEl.innerHTML = `
    <div class="eyebrow">Discovery &amp; mapping</div>
    <h2 style="margin:6px 0 8px;font:600 22px 'Space Grotesk',sans-serif">Coverage before prices.</h2>
    <p class="muted" style="margin:0 0 16px">Retailer × pack approved-mapping matrix. Prices stay secondary until collection has run.</p>
    ${summary.state === 'no_discovery_run'
      ? `<div class="status-banner"><span><strong>No discovery run yet.</strong> Matrix reflects JSON mappings and rejections only.</span><span class="pill warn">Waiting</span></div>`
      : `<div class="status-banner"><span><strong>Discovery state loaded.</strong> ${summary.discovery_runs || 0} run(s) on record.</span><span class="pill good">Available</span></div>`}
    <section class="card">
      <div class="card-head"><div><h2>Retailer coverage</h2><div class="muted" style="margin-top:5px;font-size:12px">${cov.approved_mappings} approved mappings · ${cov.packs.length} packs</div></div></div>
      <div class="coverage-wrap"><table class="coverage-table"><thead><tr><th>Benchmark pack</th>${head}</tr></thead><tbody>${body}</tbody></table></div>
      <div class="legend">
        <span><span class="cell yes">✓</span> approved</span>
        <span><span class="cell no">—</span> unmapped</span>
        <span><span class="cell pend">?</span> pending / review</span>
        <span><span class="cell ch">!</span> challenge</span>
        <span><span class="cell rej">×</span> rejected / do-not-map</span>
      </div>
    </section>`;
  viewEl.querySelectorAll('tr[data-pack]').forEach(tr => tr.addEventListener('click', () => go('pack', { catalog_id: tr.dataset.pack })));
}

async function renderCollection() {
  const data = await api('/api/collection');
  viewEl.innerHTML = `
    <div class="eyebrow">Collection health</div>
    <h2 style="margin:6px 0 16px;font:600 22px 'Space Grotesk',sans-serif">Latest run by retailer</h2>
    <div class="card"><div class="health-list">${healthRows(data.health)}</div></div>
    <p class="muted" style="margin-top:14px;font-size:12px">Workspace: <span class="mono">${esc(data.workspace_state)}</span> · DB <span class="mono">${esc(data.database.path)}</span>
    ${data.database.exists ? (data.database.openable ? ' (openable)' : ' (unreadable)') : ' (missing)'}
    </p>`;
}

function slotHtml(cell, pack) {
  const name = `<div class="rname">${esc(cell.display_name)}</div>`;
  if (cell.state === 'observed') {
    const extras = [];
    if (cell.clubcard_price) extras.push(`<div class="sub">Clubcard ${money(cell.clubcard_price)}</div>`);
    if (cell.drs_deposit) extras.push(`<div class="sub">+ ${money(cell.drs_deposit)} DRS refundable</div>`);
    if (cell.component_unit_price && pack.pack_count > 1) {
      const unit = pack.package_type === 'can' ? 'can' : 'unit';
      extras.push(`<div class="sub">${money(cell.component_unit_price)} / ${unit}</div>`);
    }
    if (cell.source_scope) extras.push(`<div class="sub">${esc(cell.source_scope)}</div>`);
    if (cell.observed_at) extras.push(`<div class="sub mono">${esc(cell.observed_at)}</div>`);
    return `<div class="slot">${name}<div class="price ${cell.is_best ? 'best' : ''}">${money(cell.displayed_price)}</div>${extras.join('')}</div>`;
  }
  if (cell.state === 'last_seen') {
    return `<div class="slot">${name}<div class="state last">Last seen</div><div class="sub mono">${esc(cell.last_seen_at || cell.observed_at || '')}</div><div class="sub">Not shown as current</div></div>`;
  }
  if (cell.state === 'temporarily_unavailable') {
    return `<div class="slot">${name}<div class="state temp">Temporarily unavailable</div><div class="sub">Collection error — not inventory</div></div>`;
  }
  if (cell.state === 'not_available') {
    return `<div class="slot">${name}<div class="state na">Not available</div><div class="sub">No approved mapping</div></div>`;
  }
  return `<div class="slot">${name}<div class="state awaiting">Awaiting price</div><div class="sub">Mapped · no observation yet</div></div>`;
}

async function renderFeed(focus) {
  const q = focus ? `?catalog_id=${encodeURIComponent(focus)}` : '';
  const data = await api('/api/feed' + q);
  if (!data.packs.length) {
    viewEl.innerHTML = `
      <div class="eyebrow">Exact-pack comparison</div>
      <h2 style="margin:6px 0 12px;font:600 22px 'Space Grotesk',sans-serif">Consumer feed preview</h2>
      <div class="card empty"><div class="empty-icon">€</div><strong>No comparable packs yet</strong>
      Include packs need at least one approved Catalog Mapping. Real repository data only.</div>
      <div class="rule">${esc(data.standing_rule)}</div>`;
    return;
  }
  viewEl.innerHTML = `
    <div class="eyebrow">Exact-pack comparison</div>
    <h2 style="margin:6px 0 8px;font:600 22px 'Space Grotesk',sans-serif">What does this pack cost today?</h2>
    <p class="muted" style="margin:0 0 4px">Displayed Price ranks among currently observed prices only. Clubcard, DRS, and component unit prices are secondary.</p>
    <div class="rule">${esc(data.standing_rule)}</div>
    <div class="feed-grid">${data.packs.map(p => `
      <section class="card feed-card">
        <div class="feed-card-head">
          <div>
            <h3>${esc(p.name)}</h3>
            <div class="mono muted" style="margin-top:4px">${esc(p.pack_label)}</div>
          </div>
          <button type="button" class="small-link" data-pack="${esc(p.catalog_id)}">Pack detail →</button>
        </div>
        <div class="retailer-slots">${p.retailers.map(c => slotHtml(c, p)).join('')}</div>
      </section>`).join('')}</div>`;
  viewEl.querySelectorAll('button[data-pack]').forEach(btn => btn.addEventListener('click', () => go('pack', { catalog_id: btn.dataset.pack })));
}

async function renderPack(catalogId) {
  if (!catalogId) {
    viewEl.innerHTML = `<div class="empty"><strong>No pack selected</strong></div>`;
    return;
  }
  const p = await api('/api/pack/' + encodeURIComponent(catalogId));
  titleEl.textContent = p.name;
  viewEl.innerHTML = `
    <div class="back"><button type="button" class="small-link" id="back-catalog">← Benchmark Catalog</button>
      · <button type="button" class="small-link" id="to-feed">Consumer feed for this pack →</button></div>
    <div class="eyebrow">Pack detail</div>
    <h2 style="margin:6px 0 4px;font:600 22px 'Space Grotesk',sans-serif">${esc(p.name)}</h2>
    <div class="mono muted" style="margin-bottom:16px">${esc(p.catalog_id)} · ${p.pack_count}×${p.unit_size_ml}ml ${esc(p.package_type)}</div>
    <div class="detail-grid">${p.retailers.map(r => {
      const price = r.current_observation ? money(r.current_observation.displayed_price) : null;
      const last = r.last_seen ? r.last_seen.observed_at : null;
      return `<div class="detail-cell">
        <h3>${esc(r.display_name)}</h3>
        <div>Mapping: <strong>${esc(r.mapping_state)}</strong></div>
        <div style="margin-top:4px">Collection: <strong>${esc(r.collection_state)}</strong></div>
        <div style="margin-top:4px">Observation: <strong>${esc(r.observation_state)}</strong></div>
        ${price ? `<div style="margin-top:8px" class="price">${price}</div>` : ''}
        ${r.current_observation && r.current_observation.clubcard_price ? `<div class="sub muted">Clubcard ${money(r.current_observation.clubcard_price)}</div>` : ''}
        ${r.current_observation && r.current_observation.drs_deposit ? `<div class="sub muted">+ ${money(r.current_observation.drs_deposit)} DRS</div>` : ''}
        ${last && r.observation_state === 'not_seen_since' ? `<div class="muted" style="margin-top:8px">Last seen ${esc(last)}</div>` : ''}
        ${r.latest_result && r.latest_result.status === 'source_error' ? `<div class="muted" style="margin-top:8px">Latest result: source_error</div>` : ''}
        ${r.mapping && r.mapping.expected_product_name ? `<div class="mono muted" style="margin-top:8px">${esc(r.mapping.expected_product_name)}</div>` : ''}
      </div>`;
    }).join('')}</div>`;
  document.getElementById('back-catalog').addEventListener('click', () => go('catalog'));
  document.getElementById('to-feed').addEventListener('click', () => go('feed', { focus: catalogId }));
}

async function render() {
  const { view, catalog_id, focus } = route();
  const navView = view === 'pack' ? 'catalog' : view;
  setNav(navView);
  viewEl.innerHTML = `<div class="muted" style="padding:24px 0">Loading…</div>`;
  try {
    if (view === 'overview') await renderOverview();
    else if (view === 'retailers') await renderRetailers();
    else if (view === 'catalog') await renderCatalog();
    else if (view === 'discovery') await renderDiscovery();
    else if (view === 'collection') await renderCollection();
    else if (view === 'feed') await renderFeed(focus || catalog_id);
    else if (view === 'pack') await renderPack(catalog_id);
    else await renderOverview();
  } catch (err) {
    viewEl.innerHTML = `<div class="card empty"><strong>Failed to load view</strong>${esc(err.message || err)}</div>`;
  }
}

document.querySelectorAll('.side-nav button').forEach(btn => {
  btn.addEventListener('click', () => go(btn.dataset.view));
});
addEventListener('popstate', render);
render();
"""


def _port_available(host: str, port: int) -> None:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            sock.bind((host, port))
        except OSError as exc:
            raise SystemExit(
                f"cannot bind dashboard to {host}:{port}: {exc}"
            ) from exc


def create_server(
    app: DashboardApp,
    *,
    host: str = DEFAULT_HOST,
    port: int = DEFAULT_PORT,
) -> ThreadingHTTPServer:
    handler = make_handler(app)
    return ThreadingHTTPServer((host, port), handler)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Local read-only Operator Dashboard for the beverage price feed"
    )
    parser.add_argument(
        "--repo-root",
        type=Path,
        default=None,
        help="repository root (default: discover from cwd)",
    )
    parser.add_argument(
        "--database",
        type=Path,
        default=None,
        help="SQLite path (default: DRINKS_DATABASE or data/feed.sqlite)",
    )
    parser.add_argument(
        "--catalog",
        type=Path,
        default=None,
        help="Benchmark Catalog JSON (default: data/catalog.json)",
    )
    parser.add_argument(
        "--mappings",
        type=Path,
        default=None,
        help="Catalog Mappings JSON (default: data/mappings.json)",
    )
    parser.add_argument(
        "--rejections",
        type=Path,
        default=None,
        help="Rejections JSON (default: data/rejections.json)",
    )
    parser.add_argument(
        "--host",
        default=DEFAULT_HOST,
        help=f"bind address (default {DEFAULT_HOST}; keep loopback-only)",
    )
    parser.add_argument(
        "--port",
        type=int,
        default=int(os.environ.get("DRINKS_DASHBOARD_PORT", DEFAULT_PORT)),
        help=f"port (default {DEFAULT_PORT})",
    )
    parser.add_argument(
        "--no-browser",
        action="store_true",
        help="do not open the system browser (agents/CI)",
    )
    args = parser.parse_args(argv)

    try:
        repo_root = (
            args.repo_root.resolve()
            if args.repo_root
            else read.resolve_repo_root()
        )
    except FileNotFoundError as exc:
        print(str(exc), file=sys.stderr)
        return 2

    catalog_path = args.catalog or (repo_root / "data" / "catalog.json")
    if not catalog_path.is_file():
        print(f"catalog.json is unreadable: {catalog_path}", file=sys.stderr)
        return 2
    try:
        load_catalog(catalog_path)
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        print(f"catalog.json is unreadable: {exc}", file=sys.stderr)
        return 2

    if args.host not in {"127.0.0.1", "localhost", "::1"}:
        print(
            f"refusing non-loopback host {args.host!r}; bind to 127.0.0.1 only",
            file=sys.stderr,
        )
        return 2

    database_path = args.database
    if database_path is None:
        database_path = read.default_database_path(repo_root)
    else:
        database_path = database_path.resolve()

    _port_available(args.host, args.port)

    app = DashboardApp(
        repo_root,
        database_path=database_path,
        catalog_path=catalog_path,
        mappings_path=args.mappings,
        rejections_path=args.rejections,
    )
    snapshot = app.load()
    server = create_server(app, host=args.host, port=args.port)
    url = f"http://{args.host}:{args.port}/"

    print(f"Operator Dashboard {url}")
    print(f"  repo_root   {repo_root}")
    print(f"  catalog     {snapshot.catalog_path}")
    print(f"  mappings    {snapshot.mappings_path}")
    print(f"  database    {snapshot.database.path} "
          f"(exists={snapshot.database.exists}, openable={snapshot.database.openable})")
    print(f"  workspace   {snapshot.workspace_state}")
    print("  mode        read-only · Ctrl+C to stop")

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
