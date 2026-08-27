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

## Operator Dashboard

Local, read-only Admin Dashboard over the real Benchmark Catalog, Catalog
Mappings, discovery/collection state, and Price Observations. Includes a
**Raw listings** view (every collection decision with its reason, plus raw
scraped candidates) for chasing products that never reach the curated views.
Includes a simple Consumer Feed Preview. Missing SQLite / empty runs show
truthful empty states (no synthetic prices). Binds to `127.0.0.1:8765` only.

```sh
.venv/bin/python run_dashboard.py
# or:
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

`make up` rebuilds the images first. That is deliberate: containers run code
copied at build time, so restarting without rebuilding silently executes stale
code (this once kept the pack-alias fix out of live runs). After changing
code, always `make up` (or `make build && docker compose up -d`).

All services share the `data` volume mounted at `/data` with
`DRINKS_DATABASE=/data/feed.sqlite`; copy `.env.example` to `.env` and fill in
credentials. Never commit `.env` or secrets.
