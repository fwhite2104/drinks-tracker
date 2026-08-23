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
```

Every command's `--database` defaults to `data/feed.sqlite` and honours
`DRINKS_DATABASE`. See `.env.example` for credential variables.

## Operator Dashboard

Local, read-only Admin Dashboard over the real Benchmark Catalog, Catalog
Mappings, discovery/collection state, and Price Observations. Includes a simple
Consumer Feed Preview. Missing SQLite / empty runs show truthful empty states
(no synthetic prices). Binds to `127.0.0.1:8765` only.

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
`/health`, `/coverage`. Local/internal use only; no authentication.

## Docker

```sh
make build   # one image: python:3.11-slim + supercronic
make up      # collector (every 4h), discovery (03:00 UTC), api (:8000)
make serve   # start only the read-only API
make collect discover review report ingest-basketwatch dashboard ARGS="..."
```

All services share the `data` volume mounted at `/data` with
`DRINKS_DATABASE=/data/feed.sqlite`; copy `.env.example` to `.env` and fill in
credentials. Never commit `.env` or secrets.
