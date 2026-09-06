# Project Status

**Snapshot: 2026-09-06.** This file is a committed snapshot; the live issue
tracker and decision trail are in `.scratch/` (gitignored, local-only).
Update this file when the state materially changes, and re-run `graft build`
after code changes.

## What the project is

Irish drink-price tracker. A **Benchmark Catalog** (~100 soft-drink packs) is
the ground truth: every pack gets a current price observation at every viable
Tier-1 retailer (Dunnes, SuperValu, Tesco; Lidl/Aldi deferred) or an explicit
exclusion — the **strict bar**. A React Native app consumes the resulting
feed. Vocabulary: [`../CONTEXT.md`](../CONTEXT.md); intent: [`../goal.md`](../goal.md).

## Efforts and their state

| Effort | Scope | State |
|---|---|---|
| `beverage-price-feed` | Core engine: collector, catalog, discovery, review CLI, feed API, dashboard, trace, canary | **Done** — all 13 tickets resolved; running in production |
| `beverage-feed-audit-remediation` | Hardening: testability, run state, retention, rate limiting, canary/release gate | **Done** — all 10 tickets resolved |
| `data-collection-stack` | Per-retailer extraction tech picks | **Done** — n8n pick formally superseded by GitHub Actions (ff-06) |
| `full-feed-coverage` | The grind: map → observe → automate everything, five retailers | **Active** — see below |
| `mobile-app` | Consumer React Native/Expo client | **Code done (tickets 1–14)**; resolution blocked on named human steps (10/11/15 + on-device pass) |

## Coverage (2026-09-06, `python -m beverage_feed report`)

- **84/500 cells approved** overall: dunnes **41/100 (77%)**, supervalu
  **35/100 (40%)**, tesco **8/100** (discovery gap, not verdicts), lidl/aldi
  **0** (deferred).
- **Comparable packs** (≥2 retailers approved): **30/100** (was 11 before the
  09-04 sprint).
- `research/comparability-scorecard-2026-09-04.md`: 19 packs have ≥2-retailer
  evidence, 74 single-retailer, 7 no-signal.

## Key decisions (all resolved)

- **GitHub Actions is the scheduler** (ff-06/ff-08): daily collection, no
  alerting, feed updates itself unattended; all retailer egress on CI
  (home IP blocked by Tesco's Akamai, 403'd by Dunnes).
- **Translation layer, not bar-lowering** (ff-04/ff-12/13/14): curated Brand
  Alias dictionary, junk relevance gate, evidence classes A–D, term-expansion
  rediscovery — the exact-pack bar itself never moved.
- **Review via dashboard sprints** (ff-05, all five verdict questions
  answered): agent sprints apply evidence-cited verdicts through the real
  CLI seam; the operator spot-checks.
- **Lidl/Aldi deferred** (goal.md amendment 2026-09-04): adapters built and
  tested; automated runs yield keyword noise.
- **Scope amendment standing preference**: strict standards, no weak mappings
  for speed; dashboard review only (terminal review of batches rejected).

## Open work

- **ff-07 coverage grind** (in progress): Feilim sprints on ~12 genuinely
  ambiguous candidates + ~35 pending dunnes zero-candidate cells; Tesco CI
  discovery runs; then the grind loop on remaining cells.
- **ff-15 global not-a-beverage rejection** — specified, not implemented
  (junk dies once across a retailer, not once per cell).
- **ff-16 catalog variant expansion** — decided (7UP Sugar Free & siblings
  become cells), not implemented.
- **Catalog size vs comparability** — open question on the ff map: shrink the
  catalog to the comparable core (scorecard says evidence supports ~30), or
  stay at 100 and grind? Interacts with ff-16.
- **Mobile human chain**: domain + Cloudflare tunnel (10), EAS builds (11),
  on-device pass (12–14), store accounts (15) — `scripts/setup-accounts.sh`.

## Suggested review order for an agent

1. `.scratch/full-feed-coverage/map.md` — destination + full decision index.
2. `CONTEXT.md` + this repo's ADRs (`docs/adr/` if present).
3. ff-07 — the grind loop and what's left on it.
4. ff-15/16 and the catalog-size question.
5. [`../CONTRIBUTING.md`](../CONTRIBUTING.md) before touching `beverage_feed/`.
