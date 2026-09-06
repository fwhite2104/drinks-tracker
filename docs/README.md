# Documentation

Committed documentation for the drinks-tracker project. The living issue
tracker and decision trail live in `.scratch/` (gitignored, local-only);
this `docs/` tree is the committed, teammate-facing record.

## Start here

| Doc | What it covers |
|---|---|
| [`../README.md`](../README.md) | Install, test suite, CLI quick reference |
| [`../goal.md`](../goal.md) | Product intent, scope, definition of success |
| [`../CONTEXT.md`](../CONTEXT.md) | Domain glossary (Benchmark Catalog, Discovery Run, Review Decision, Brand Alias…) |
| [`../CONTRIBUTING.md`](../CONTRIBUTING.md) | Coding standards, environment, money/time/SQLite rules, test policy |
| [`architecture.md`](architecture.md) | System overview: pipeline stages and the module map |
| [`data-model.md`](data-model.md) | SQLite schema, durable JSON files, batch artifacts, release snapshots |
| [`retailers.md`](retailers.md) | Per-retailer access routes, quirks, and known limitations |
| [`discovery-and-review.md`](discovery-and-review.md) | Discovery → classification → review sprints → durable decisions |
| [`operations.md`](operations.md) | Scheduled automation, CI egress, the VM, deployment, canary, dashboards |
| [`mobile.md`](mobile.md) | The React Native/Expo consumer app: contract, caching, builds |
| [`status.md`](status.md) | Current project state snapshot (dated; the live tracker is `.scratch/`) |

## Reading order for a new contributor

1. `goal.md` — what this is for.
2. `CONTEXT.md` — the vocabulary (everything downstream uses these terms).
3. `docs/architecture.md` — how the pieces fit.
4. `docs/status.md` — where the project stands right now.
5. `CONTRIBUTING.md` — before touching `beverage_feed/` or `tests/`.

## Operational runbooks

- Cloudflare Tunnel deployment: [`../deploy/README.md`](../deploy/README.md)
- Operator account/domain setup wizard: `scripts/setup-accounts.sh`
- Agent working conventions: [`agents/`](agents/) (issue tracker, triage labels, domain docs)
