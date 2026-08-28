# Ingress route split — public vs operator

One API host (`api.<your-domain>` → the compose `api` service on port 8000)
carries both audiences; the split is enforced by **Cloudflare Access
self-hosted applications matching path prefixes** on that hostname
(see `README.md` §4). The API itself has no auth (spec §2, ticket-02) —
everything below is enforced at the tunnel/edge.

## Public — anonymous, edge rate-limited

Consumer-facing read routes the mobile app calls:

| Route | Handler | Purpose |
|---|---|---|
| `GET /consumer/feed` | `beverage_feed.api:consumer_feed` | §4 Exact-Pack Comparison feed (five consumer states, no operator diagnostics) |
| `GET /catalog` | `catalog` | Benchmark Catalog listing |
| `GET /prices/current` | `prices_current` | current prices |
| `GET /prices/history` | `prices_history` | price history |
| `GET /last-seen` | `last_seen_for` | last-seen lookup |
| `GET /health` | `health` | status + table counts (also used by `make deploy-check`) |

Anything **not** listed as operator below stays public under this split.

## Operator — Cloudflare Access required

Diagnostics / collection-internals endpoints. Every one is a Cloudflare
Access self-hosted app entry:

| Route | Handler | Why protected |
|---|---|---|
| `/runs*` | `runs` | collection run history, internal ids |
| `/results*` | `results` | per-item collection results, source references |
| `/candidates*` | `candidates` | discovery candidates, raw product refs |
| `/coverage*` | `coverage` | internal coverage accounting |

Plus the optional Operator Dashboard host (`operator.<your-domain>`,
uncommented block in `config.yml.example`) — that host is Access-protected
wholesale if enabled.

## Invariants

- Operator diagnostics (run ids, source refs, raw candidates) must never
  appear in public responses — `/consumer/feed` already whitelists its fields
  (spec §4; pinned by tests in `tests/`).
- No auth on public routes for v1. If auth is ever added, ticket-02 records
  the preferred shape (token check at the tunnel or a JS BFF).
- Rate limiting is the only public-route protection; see `README.md` §5.
