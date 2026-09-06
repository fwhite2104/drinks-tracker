# Operations

What runs where, on what schedule, and how to verify it. Scheduler and
monitoring decisions are recorded in ff-06 (resolved): **GitHub Actions is
the scheduler; daily cadence; no alerting (hobby project).**

## Topology

```
GitHub Actions (all retailer egress)          Proxmox VM (zero retailer egress)
├── collect.yml   daily collection    ──batch──▶ pull-batch (4-hourly supercronic)
├── rediscover.yml manual per-retailer──artifact─▶ discovery_merge into live DB
│   discovery dispatch                         ▲
├── canary.yml    release gate                 │ compose stack:
└── ci.yml        tests                        └─ api (read-only HTTP API)
                                                  dashboard (operator, LAN)
                                                  cloudflared → api.<domain>
        (home network: never touches a retailer)
```

## Scheduled automation (live)

- **`collect.yml` — collection, once daily** (`15 4 * * *`; ff-06 cadence) +
  `workflow_dispatch` for retries. Collects all configured retailers into a
  disposable `ci-feed.sqlite`, exports `collection-batch.json` for the
  latest run, uploads it as an artifact. **The batch ships even on failure** —
  `continue-on-error` on the collect step; a `source_error` outcome is
  truthful data.
- **VM pull**: `crontabs/collector.cron` (supercronic in docker-compose)
  runs `pull-batch` at :40 every 4h — downloads the latest successful
  `collect` artifact via the GitHub API (fine-grained PAT, `actions:read`)
  and ingests it **whole-run idempotent by `run_id`**; re-pulling a batch is
  a no-op. The VM performs **zero retailer egress**.
- **Liveness**: `freshness` runs daily on the VM (passive, always exits 0):
  one line per retailer, freshest observation age — the frozen-collection
  signature (mappings but `None` observations) is visible here.
- **No alerting** by explicit operator ruling; GitHub's default failure
  emails land in the repo owner's inbox and nothing further was built.
- Superseded and retired: the VM-side discovery cron (discovery moved to CI
  2026-09-04) and the old 4-hourly collect cadence.

## Discovery egress (manual, dispatched)

- **`rediscover.yml`** — per-retailer `workflow_dispatch` (choice input);
  seeds the CI database from a **release snapshot** tag
  (`feed-snapshot-*`, e.g. `feed-snapshot-2026-09-03`), runs first-discovery
  or re-discovery for that retailer, uploads the partial DB regardless of
  outcome. The operator dispatches per retailer when there is discovery work;
  artifacts are merged into the live DB with `discovery_merge` (which also
  restores `catalog_candidates` rows and applies reconcile guards).
- Cut a fresh snapshot whenever local discovery state has advanced and CI
  should continue from it.

## CI housekeeping

- **`ci.yml`** — test suite on push (never calls live endpoints).
- **`canary.yml`** — the live-retailer canary + release gate; **manual
  trigger only, never scheduled** (deliberately not part of any cadence).

## Local CLI (operator)

```sh
python -m beverage_feed                 # collect (local dev only — CI owns prod egress)
python -m beverage_feed discovery       # budgeted discovery (explicit --retailer only for lidl/aldi)
python -m beverage_feed review ...      # approve/reject/replace/do-not-map/challenges/classify
python -m beverage_feed report          # coverage vs the strict bar
python -m beverage_feed freshness       # per-retailer freshest-observation age
python -m beverage_feed trace ...       # trace a pack/reference through every stage
python -m beverage_feed canary          # manual live canary + release gate
python -m beverage_feed pull-batch      # pull + ingest the latest CI batch
python -m beverage_feed dashboard       # local operator dashboard (read-only)
```

Per-retailer collection decisions log one line each
(`decision=observed|unmapped|not_found|source_error` + reason). See the root
`README.md` for the full CLI reference, including `trace`.

## Deployment (public API)

The compose stack runs on the Proxmox VM; the public surface is a **Cloudflare
Tunnel** from that VM — `api.<domain>` → `http://api:8000`, no auth v1
(rate-limited at the edge), operator routes (`/runs*`, `/results*`,
`/candidates*`, `/coverage*`) behind Cloudflare Access.

The full mechanical runbook (tunnel create, DNS route, Access apps, WAF
rate-limit rule, off-LAN verification) lives in
[`../deploy/README.md`](../deploy/README.md); `deploy/config.yml.example` is
the tunnel config template and `make deploy-check BASE_URL=…` (via
`deploy/healthcheck.sh`) verifies `/health` and `/consumer/feed` from
outside the LAN. The human steps that remain are tracked in
`.scratch/mobile-app/issues/10-public-deployment-cloudflared.md` (gitignored
tracker).

## Failure playbook

| Symptom | Check |
|---|---|
| Retailer observations frozen (`freshness` shows `None` or rising age) | `collect.yml` runs + batch artifacts on GitHub; `python -m beverage_feed trace --retailer <r>` for the exact recorded reason |
| A product "found in scraping" missing from feed | `trace --catalog-id <id>` / `trace --reference <ref>` — prints mapping state, collection reasons, observations, diagnostics, raw candidates without re-scraping |
| Discovery run stopped early | failure-pause policy after repeat failures (likely IP-block); move that retailer's egress to CI via `rediscover.yml` |
| Supervalu/Tesco mapped but no observations | the frozen-collection signature — see `freshness.py` docstring; history has produced exactly this before (2026-08-27 → 09-03) |
