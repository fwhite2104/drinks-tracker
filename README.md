# Drinks Tracker

Local collection and review tools for the Irish beverage price feed.

## Retailers

Tier 1 adapters: Tesco Ireland, Dunnes Stores, SuperValu, Lidl Ireland,
Aldi Ireland. Each adapter uses first-party JSON endpoints where available;
validated source limitations are documented in the client docstrings.

## Development

Use the project-local virtual environment, then install the pinned development
checks:

```sh
.venv/bin/python -m pip install -e '.[dev]'
```

Run the complete local check suite with one command:

```sh
.venv/bin/python -m pytest
```

The suite uses captured fixtures and test doubles; it never calls live retailer
endpoints. The command fails when no tests are discovered.

## CLI

```sh
python -m beverage_feed                 # collect prices (all configured mappings)
python -m beverage_feed discovery       # budgeted catalog-mapping discovery
python -m beverage_feed review          # operator review (approve/reject/replace/challenges)
python -m beverage_feed report          # discovery coverage reporting
python -m beverage_feed basketwatch     # optional external-source ingest
python -m beverage_feed dashboard       # local Operator Dashboard (read-only)
python -m beverage_feed trace           # trace one product through every pipeline stage
python -m beverage_feed canary          # manual live retailer canary + release gate (never scheduled)
```

### Tracing a missing product

When a product is "found in scraping" but does not appear in the API or
dashboard, trace it through the persisted stages without re-scraping:

```sh
python -m beverage_feed trace --catalog-id coca-diet-330-8
python -m beverage_feed trace --reference 3029607      # source ref / item id / candidate id
python -m beverage_feed trace --catalog-id coca-diet-330-8 --retailer tesco
```

For the traced cell it prints: Catalog Mapping state (collection only runs
approved mappings), recent collection results with the exact recorded reason
(`unmapped` / `not_found` / `source_error`), Price Observations, collection
diagnostics, and raw Catalog Candidates. If a reference is only known to the
raw candidate store, the tracer says so and shows those candidates.

Collection also logs one line per retailer-pack decision
(`decision=observed|unmapped|not_found|source_error` plus reason) and prints a
run summary; tune with `DRINKS_LOG_LEVEL` (default `INFO`).

Every command's `--database` defaults to `data/feed.sqlite` and honours
`DRINKS_DATABASE`. See `.env.example` for credential variables.

## Live canary & release gate

Before trusting a scheduled or manual feed run, verify the configured Dunnes,
SuperValu, and Tesco routes still return valid exact-pack prices:

```sh
.venv/bin/python -m beverage_feed canary            # all three retailers
.venv/bin/python -m beverage_feed canary --retailer tesco
.venv/bin/python -m beverage_feed canary --gate-status   # release-gate verdict only
```

The canary probes **one known mapped listing per retailer** (the first
approved Catalog Mapping in `data/mappings.json`, or a pinned pack via
`--catalog-id`) and validates source identity (the observed listing is the
mapped listing), product attributes (brand/variant/pack composition), the
Displayed Price, promotions recorded separately (loyalty/clubcard), and the
DRS Deposit. Each probe runs through the real collectors into a throwaway
temp database — the canary never writes observations to the feed.

Outcomes are reported per retailer and kept apart:

- `pass` — the mapped listing was observed and every check succeeded.
- `drift` — **endpoint drift**: transport/HTTP/auth failure, a changed
  response shape, or a truncated page. Fix the route/credentials first.
- `absent` — **product absence**: the endpoint works but the mapped listing
  is gone. Refresh the Catalog Mapping instead of chasing the route.
- `invalid` — the source answered but validation failed (identity mismatch,
  composition drift, malformed price/promotion/deposit).

The canary is **manually invoked and never scheduled**: it runs where you
invoke it, or via the `workflow_dispatch`-only canary job
(`.github/workflows/canary.yml`, run from the Actions tab — same egress
rationale as collection). It is never part of CI checks and never run by the
test suite; tests cover its logic with fake clients and captured fixtures.

### Release gate

Every canary run appends its outcome per retailer to
`data/canary-gate.json` (`--gate-state` overrides; `DRINKS_CANARY_STATE`
environment override). Collection enforces the gate **opt-in**:

```sh
python -m beverage_feed --release-gate      # or DRINKS_RELEASE_GATE=1
```

After `3` consecutive canary failures for a retailer (and while the newest
failure is younger than 7 days), collection refuses that retailer with a
`release gate blocks ...` message until a canary passes. A passing canary
resets the streak immediately; a gate older than 7 days stops blocking, so
the procedure while a source is drifting is simply to keep running the
canary. Scheduled GitHub Actions collection (a disposable database with no
canary state) is unaffected.

### Refreshing captured fixtures and Catalog Mappings

1. Capture current payloads: `python -m beverage_feed canary --dump-fixtures
   /tmp/canary-fixtures` writes the scrubbed, client-normalized response
   payload per retailer.
2. Trim a payload to the mapped listing and save it over the matching
   `tests/fixtures/<retailer>_*.json` file.
3. If the listing's source identity changed (new product reference / item
   id / TPNB), update the Catalog Mapping in `data/mappings.json` in the same
   change — the canary's `identity` check pins the mapping's source
   reference against the observed listing.
4. Run `pytest` (the fixture tests pin the captured shapes) and commit.

### SuperValu store scope and Tesco API key

- **SuperValu** probes run against the same configured store as collection
  (`--supervalu-store-id` or `SUPERVALU_STORE_ID`); the store identifier is
  recorded as the result's source scope, so a canary pass is only valid for
  that store.
- **Tesco** requires `TESCO_API_KEY` (sent as the `x-apikey` GraphQL
  header). Without it the Tesco canary reports `invalid` ("not configured")
  rather than pretending the route is healthy; both credentials live in
  GitHub Actions secrets for the collection and canary workflows.

## Operator Dashboard

Local, read-only Admin Dashboard over the real Benchmark Catalog, Catalog
Mappings, discovery/collection state, and Price Observations. Includes a
**Raw listings** view (every collection decision with its reason, plus raw
scraped candidates) for chasing products that never reach the curated views.
Includes a simple Consumer Feed Preview. Missing SQLite / empty runs show
truthful empty states (no synthetic prices). Binds to `127.0.0.1:8765` only.

```sh
.venv/bin/python -m beverage_feed dashboard
.venv/bin/python -m beverage_feed dashboard --no-browser   # agents/CI
.venv/bin/python -m beverage_feed dashboard --port 8765
```

Optional XDG launcher: copy `operator-dashboard.desktop`, set `Path=` to the
repo root, and install under `~/.local/share/applications/`.

## Read-only API

```sh
.venv/bin/python -m pip install -e '.[api]'
DRINKS_DATABASE=data/feed.sqlite uvicorn beverage_feed.api:app --port 8000
```

Endpoints: `/catalog`, `/prices/current`, `/prices/history`, `/last-seen`,
`/health`, `/coverage`, `/consumer/feed`. Curated views only surface observed
prices from approved mappings. Raw, no-frills views of everything ingestion has recorded:

- `/results` — every collection result (including `unmapped`, `not_found`,
  `source_error`) with the reason it was dropped. Filters:
  `retailer`, `status`, `run_id`, `limit`.
- `/candidates` — raw scraped listings not yet tied to a Benchmark Catalog
  pack. Filters: `retailer`, `status`, `limit`.
- `/runs` — recent collection runs with their summaries.

### Consumer feed

```sh
curl "http://localhost:8000/consumer/feed"
curl "http://localhost:8000/consumer/feed?catalog_id=coca-zero-2000"
```

The mobile-app data source: one entry per pack with at least one approved
mapping, and per-retailer slots carrying the five consumer states
(`observed`, `last_seen`, `awaiting_price`, `temporarily_unavailable`,
`not_available`) with Displayed Price, Clubcard Price, DRS Deposit, and
Component Unit Price — identical semantics to the dashboard's Consumer Feed
Preview (both render through `dashboard_read.consumer_cell`). Dormant
mappings are omitted; `is_best` flags the cheapest current price; a missing
price is never a stock or retirement claim.

`/health` includes `code_mtime` (build time of the running code): if a fresh
run still behaves like old code, compare it against the latest commit — a
stale container image is the usual culprit.

Local/internal use only; no authentication.

## Docker

```sh
make build   # one image: python:3.11-slim + supercronic
make up      # build + batch pull (every 4h), discovery (03:00 UTC), api (:8000)
make serve   # start only the read-only API
make collect discover review report ingest-basketwatch dashboard ARGS="..."
```

### Collection egress

**Price collection runs on GitHub Actions** (`.github/workflows/collect.yml`,
every 4h on rotating cloud IPs) and the VM pulls the resulting batch 40
minutes later (`python -m beverage_feed pull-batch`, whole-run idempotent).
This keeps the home IP away from retailer edge blocks: Tesco's Akamai
IP-blocked this network in August 2026. Consequences:

- The VM `collector` container only pulls batches — it needs `GITHUB_TOKEN`
  (fine-grained PAT, actions:read) in `.env`.
- Retailer credentials live in GitHub Actions secrets (`TESCO_API_KEY`,
  `SUPERVALU_STORE_ID`). A missing secret fails the run loudly.
- Discovery still runs on the VM (03:00 UTC); revisit if its endpoints ever
  see the same IP-level block.
- Batch flow: CI run → `collection-batch` artifact → VM `pull-batch` →
  `data/feed.sqlite`. Re-ingesting a batch never duplicates rows.
- Before trusting a batch, run the live canary (see "Live canary & release
  gate" above) — manually, or from the manual-only `canary` workflow.

### Term-expansion rediscovery

Thin/Class-D catalog cells get a second discovery pass with alternate search
formulations (ticket 14). Where each retailer's pass runs:

- **Dunnes, SuperValu, Lidl, Aldi**: any machine with `data/feed.sqlite` —
  the VM or an operator laptop (`python -m beverage_feed discovery
  --rediscover`; its default retailer set excludes Tesco). No CI workflow,
  by design.
- **Tesco**: only through CI egress, via the manual-only
  `rediscover-tesco` workflow (`.github/workflows/rediscover-tesco.yml`).
  Dispatch from the Actions tab; inputs are `list_only` (upload the
  target-cell JSON and stop — zero retailer requests), `max_formulations`
  (default 4), `request_cap` (default 200), and `state_release_tag`. The CI
  database is fresh and stateless, so with no seed the target preview comes
  up empty: to rediscover the VM's thin cells, first attach the VM's
  `data/feed.sqlite` as a release asset (`gh release create <tag>
  data/feed.sqlite`) and pass the tag as `state_release_tag`. Artifacts:
  `rediscovery-targets` (always) and `rediscovery-db` (when the pass ran —
  the seeded DB plus this pass's searches, decisions, and re-classification).
  Discovery tables have no batch ingest; to keep the evidence, download
  `rediscovery-db` and swap it in as the VM's `data/feed.sqlite` (stop the
  stack, replace, `make up`).

`make up` rebuilds the images first. That is deliberate: containers run code
copied at build time, so restarting without rebuilding silently executes stale
code (this once kept the pack-alias fix out of live runs). After changing
code, always `make up` (or `make build && docker compose up -d`).

All services share the `data` volume mounted at `/data` with
`DRINKS_DATABASE=/data/feed.sqlite`; copy `.env.example` to `.env` and fill in
credentials. Never commit `.env` or secrets.
