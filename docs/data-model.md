# Data Model

Three layers of state, in decreasing order of authority:

1. **Durable JSON (identity)** — `data/catalog.json`, `data/mappings.json`,
   `data/rejections.json`. Operator-editable, committed, digest-pinned.
2. **SQLite (operations)** — `data/feed.sqlite` (gitignored). Evidence,
   observations, runs, diagnostics. Reconciled from the JSON, never vice versa.
3. **Artifacts (transport)** — CI batch/discovery JSON + GitHub release
   snapshots. Move state between environments; idempotent to ingest.

## Durable JSON files

| File | Contents | Notes |
|---|---|---|
| `data/catalog.json` | The Benchmark Catalog packs: `catalog_id`, `name`, `brand`, `variant`, `pack_count`, `unit_size_ml`, `package_type`, `search_term`, `aliases` | Identity ground truth; every pack is a specific sellable variant |
| `data/mappings.json` | Approved catalog↔listing mappings per retailer, with source refs and a digest | `load_mappings` validates against the discovery validator; `tests/test_mappings_file.py` pins the committed file so it can't silently drift |
| `data/rejections.json` | Listing rejections (candidate×cell) + cell exclusions | Durable decision trail; `reconcile_json_decisions` applies it to the DB with guards (e.g. a competing-candidate rejection never demotes an approved cell) |

## SQLite schema (`data/feed.sqlite`)

Schema is versioned via `PRAGMA user_version`; migrations in
`collector.py:_MIGRATIONS` are idempotent and non-destructive.

**Collection side** (`collector.py`):

- `catalog_packs` — catalog identity mirrored from `catalog.json`
- `catalog_mappings` — approved mapping per (catalog_id, retailer)
- `catalog_candidates` — raw retailer listings ever seen; `status` is
  "last event wins" globally; per-cell truth lives in the discovery tables
- `collection_runs` — one row per collection run (run_id, status, counts)
- `collection_results` — per (run, pack, retailer) outcome with the exact
  recorded reason (`observed` / `unmapped` / `not_found` / `source_error`)
- `collection_diagnostics` — per-event diagnostic lines, request metadata
- `price_observations` — the feed's core fact: displayed price, Clubcard
  price, DRS deposit, component unit price, price-per-litre, `observed_at`,
  all money as text/Decimal, one observation per (run, pack, retailer, scope)
- `retailers` — retailer registry

**Discovery side** (`discovery.py`, added by `ensure_discovery_schema`
without touching observations):

- `discovery_runs`, `discovery_attempts`, `discovery_search_history` —
  what was searched, when, with which budget
- `discovery_cells` — one per (pack, retailer): current review state
  (`approved` / `rejected` / `inconclusive` / `pending` / `do_not_map` …)
- `discovery_candidate_cells` + `discovery_candidate_evidence` — which
  candidates a cell surfaced and the normalized evidence for each
- `discovery_rejections` — listing rejections per candidate identity
  (INSERT OR IGNORE guards against same-second UNIQUE collisions)
- `discovery_state_transitions` — audit trail of cell-state changes
- `discovery_identity_links`, `discovery_diagnostics`

Key indexes: `catalog_candidates_retailer_identity` (unique per retailer +
identity_key) and the observation cell index (one observation per run,
retailer, pack, source scope).

## Batch artifacts (CI → VM)

- `collect.yml` exports `collection-batch.json` for the run and uploads it as
  a GitHub artifact (even when the run is partial — `source_error` is
  truthful data).
- The VM's `pull-batch` downloads the latest successful artifact via the
  GitHub API (fine-grained PAT, `actions:read`) and ingests it
  **whole-run idempotent by `run_id`**: re-pulling the same batch is a no-op,
  and cross-environment re-ingest is skipped correctly.
- Discovery artifacts from `rediscover.yml` are merged by `discovery_merge`,
  which copies discovery tables *and* `catalog_candidates` (the registry the
  approve/reject validators check) and applies reconcile guards.

## Release snapshots

`feed-snapshot-*` git tags carry a `feed.sqlite` snapshot (e.g.
`feed-snapshot-2026-09-03`) used to seed CI discovery runs, which start from
the live DB's state rather than an empty one. Cut one whenever discovery
state has meaningfully advanced and CI should continue from it.

## Consumer feed shape

`GET /consumer/feed` returns packs + retailers with, per pack×retailer, the
server-owned state machine: `observed`, `last_seen`, `awaiting_price`,
`temporarily_unavailable`, `not_available` — plus `label`, displayed price,
Clubcard price, DRS deposit, component unit price, and observation dates.
This shape is the mobile app's contract (see [`mobile.md`](mobile.md)).
