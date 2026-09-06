# Architecture

How drinks-tracker fits together: one Python backend (`beverage_feed/`), one
React Native consumer app (`mobile/`), GitHub Actions for all retailer egress,
one Proxmox VM running the compose stack and public tunnel.

## The pipeline

```
Benchmark Catalog (data/catalog.json, ~100 packs)
        │
        ▼
   DISCOVERY ── searches retailers for each not-yet-mapped cell,
   │            normalizes evidence into catalog_candidates +
   │            discovery cells/evidence (records what was searched;
   │            creates no observations)
   ▼
   REVIEW ────── operator or agent-sprint decisions (approve / reject /
   │            do-not-map / exclude) applied via the real review CLI seam;
   │            durable JSON first (mappings.json, rejections.json),
   │            reconciled into the database
   ▼
   COLLECTION ── runs over approved mappings only; turns mappings into
   │            Price Observations (price, Clubcard, DRS deposit, dates)
   ▼
   BATCH ─────── CI exports the run as collection-batch.json; the VM pulls
   │            and ingests it whole-run idempotent by run_id
   ▼
   FEED API ──── read-only HTTP API; /consumer/feed carries the server-owned
   │            five-state machine per retailer×pack
   ▼
   MOBILE APP ─ renders; never derives state or invents prices
```

Core rule: **discovery finds mappings, collection turns mappings into
observations.** Nothing skips a stage; the strict bar (every pack observed or
explicitly excluded at every viable retailer) is enforced per stage.

## Module map (`beverage_feed/`)

| Module | Role |
|---|---|
| `collector.py` | Collection seam + Dunnes VTEX/grocery-gateway client; the SQLite schema (`ensure_schema`, versioned migrations), run/observation tables, `collect_*_one` per retailer |
| `finders.py` | Pure per-retailer listing finders + money extractors (Clubcard, DRS deposit); no persistence |
| `source_http.py` | Shared resilient transport: retry classification, bounded backoff + jitter, `Retry-After`, per-retailer spacing, circuit breaker; diagnostics never carry credentials |
| `aldi.py`, `lidl.py` | Thin first-party JSON clients (Spryker Glue / storefront search), same fetcher contract, drop-in for the collector |
| `matching.py` | Curated Brand Alias dictionary + alias resolution, search formulations, name matching |
| `discovery.py` | `DiscoveryStore`: candidate store, discovery cells/evidence, durable decision application, reconciliation |
| `discovery_adapters.py` | Retailer discovery seams: source → normalized evidence, request accounting, completeness classification |
| `discovery_run.py` | Budgeted discovery runs, rediscovery targets/pass (term expansion) |
| `discovery_classify.py` | Evidence classes A–D per candidate-cell; junk gate; cell rollup; sprint batches |
| `discovery_decisions.py`, `discovery_cli.py` | The review seam: approve/revoke/reject/do-not-map/replace/challenges/classify |
| `discovery_merge.py` | Merge CI discovery artifacts into the live DB (including candidate rows and reconcile guards) |
| `discovery_report.py` | `report` — coverage vs the strict bar |
| `scorecard.py` | Read-only catalog comparability scorecard (catalog × candidates × mappings × rejections) |
| `dashboard_read.py` | Read-only workspace snapshot (JSON identity + optional SQLite probe) feeding both dashboards |
| `dashboard.py` | Operator Dashboard server (read-only) |
| `dashboard_sprint.py` | Sprint mode: keyboard review queue, batch actions, written through the real CLI seam only |
| `api.py` | HTTP API: consumer routes (`/consumer/feed`, `/catalog`, `/prices/*`, `/last-seen`, `/health`) and operator routes (`/runs`, `/results`, `/candidates`, `/coverage`) |
| `batch.py` | `export-batch` / `ingest_batch` / `pull_latest_batch` (GitHub artifact → DB) |
| `freshness.py` | Passive liveness: freshest observation age per retailer |
| `canary.py` | Manual live-retailer canary + release gate (never scheduled) |
| `trace.py` | Trace one catalog pack / source reference through every persisted pipeline stage |
| `basketwatch.py` | Optional external-source ingest (secondary to primary adapters) |
| `money.py` | Money serialization/parsing (Decimal everywhere, no floats) |

## The mobile app (`mobile/`)

React Native + Expo (TypeScript), one codebase for Android + iOS, EAS cloud
iOS builds. Consumes `/consumer/feed` only. See [`mobile.md`](mobile.md).

## Design invariants

- **Honesty over coverage**: a missing price is exactly one of five truthful
  states; `source_error` batches are still truthful data and always ship.
- **Server owns the state machine**; the app (and any client) only renders
  the server-provided `label` and state — never re-derives them.
- **Durable decisions are JSON-first**: `mappings.json` / `rejections.json`
  are the operator-editable identity of record; the database is reconciled
  from them, never the other way around.
- **Zero home-IP retailer egress**: everything retailer-facing runs on GitHub
  Actions (see [`operations.md`](operations.md)).
- **No synthetic data anywhere**: offline cache holds raw API responses only;
  external ingests (basketwatch) are labelled by source.

## Where things live

| Path | Contents |
|---|---|
| `data/` | `catalog.json`, `mappings.json`, `rejections.json`, `feed.sqlite` (gitignored DB) |
| `.github/workflows/` | `ci.yml`, `collect.yml`, `rediscover.yml`, `canary.yml` |
| `crontabs/` | VM supercronic entries (pull-batch, freshness) — zero retailer egress |
| `deploy/` | Cloudflared tunnel runbook + config + healthcheck |
| `.scratch/` | Wayfinder maps + issue tracker (gitignored, local) |
| `graft/` | Generated repo context graph (gitignored, run `graft build`) |
