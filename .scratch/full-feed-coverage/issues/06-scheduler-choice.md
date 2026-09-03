# Choose the scheduler for multi-site collection

Type: grilling
Status: resolved
Blocked by: 01, 02, 03

## Question

Plain cron won't cut it: five heterogeneous sites each with their own failure modes need orchestration, retries, and gap isolation across intervals. The earlier [data-collection-stack map](../../data-collection-stack/map.md) picked n8n — revisit that explicitly now that multi-site reality is known. Decide: scheduler mechanism (n8n workflows vs cron-with-per-retailer-wrappers vs systemd timers vs something else), interval per retailer, per-retailer retry/error handling that doesn't let one site's outage stall the rest, and what monitoring/alerting exists when a run ends `partial`. Decision only — implementation becomes the automation slice.

## Recommendation (draft — awaiting Feilim)

*Prepared by the agent scout; facts gathered from resolved ticket 11, `.github/workflows/collect.yml`, `crontabs/`, `docker-compose.yml`, and the `beverage_feed` CLI entry points (`__main__.py`). This section does not resolve the ticket — Feilim decides.*

### The ground truth this ticket must now account for

Since this ticket was opened, [ticket 11](11-github-actions-collection-egress.md) landed and the operator widened it: **all five retailers collect on GitHub Actions** (`collect.yml`, cron `15 */4 * * *`), because the home IP is Akamai-blocked for Tesco and CI isolates every retailer from home-IP reputation damage. The VM's crontab no longer collects — it only `pull-batch`es at :40 every 4h and runs discovery at 03:00. So the old [data-collection-stack ticket 06](../../data-collection-stack/issues/06-pipeline-orchestrator.md) pick of **n8n was a VM-era decision and is effectively moot**: n8n self-hosted on the VM cannot fix the egress problem (it would fire requests from the blocked IP), and n8n in the cloud is new infrastructure to babysit for a job GitHub Actions already does.

### 1. The real options today

1. **GitHub Actions *is* the scheduler** (recommended). Collection stays where it is; evolve the existing workflow rather than add an orchestrator. `super cronic` on the VM keeps only pull-batch + discovery. This is the status quo plus three small additions (per-retailer scheduling, run-level retry policy, alerting) — no new system, no new secrets surface, no new failure mode between scheduler and scraper.
2. **n8n, revisited**: rejected. Self-hosted n8n on the VM cannot run collection (egress constraint, resolved ticket 11) and can only orchestrate the CI run *through* GitHub's own API — a wrapper around the thing we'd already be using. It adds a second scheduler whose value over the workflow YAML is nil at this scale.
3. **systemd timers / plain cron on the VM**: rejected for collection by the same egress constraint (would fire from the blocked home IP). Fine for what it already does — pull-batch, discovery — and supercronic in docker-compose already covers that.
4. **External scheduler-as-a-service** (CronITOR-style triggers hitting `workflow_dispatch`): unnecessary — GitHub's own `schedule` trigger already works and is proven by a live run.

### 2. Comparison against the ticket's stated needs

| Need (ticket text) | Status today | Gap / action |
|---|---|---|
| **Per-retailer intervals** | One 4h cron for all five (`collect.yml`), single run | Small YAML change: `strategy.matrix` over retailers + per-retailer `schedule` entries (or dispatch input). Each retailer gets its own interval and its own artifact. |
| **Retry / gap isolation** | Already strong: request-level exponential backoff (`_retrying_fetcher` in `collector.py`); one retailer's `source_error` is recorded as truthful data and does **not** stall the others; run exits `partial`, batch still ships (commit f0a1d7b); ingest is idempotent by `run_id`; latest-result-wins means gaps are visible, not silent | Only missing layer is **run-level** retry (a whole failed run today needs a manual `workflow_dispatch`). Could add auto-retry, but one 4h-cadence retry naturally re-covers a transient miss — likely unnecessary. |
| **Partial-run alerting** | **The real gap.** Nothing alerts today: no notification channel anywhere in the repo; a partial run ships a truthful batch but nobody is paged. GitHub's default failure email to the repo owner is the floor, not a design. | Needs a decision (see questions): GitHub failure notifications + a VM-side **feed-staleness watchdog** (cron asserting freshest observation age, e.g. via `python -m beverage_feed report`) is the shape I'd draft. |
| **Tesco CI-egress constraint** (resolved ticket 11) | Binding: collection must not run from the home IP | This single constraint eliminates options 2 and 3 for collection outright. Any scheduler choice must either *be* GitHub Actions or sit on top of it. |

### 3. Recommended choice

**GitHub Actions is the scheduler.** Reasoning:

- The egress decision (ticket 11) already forced collection into CI; the scheduling problem is therefore already 80% solved there — schedule trigger, per-run isolation, artifacts, manual dispatch, and a full audit log of every run all exist today.
- The ticket's remaining needs decompose into two small YAML changes (matrix + per-retailer cron lines) and one genuinely new piece (alerting), none of which benefit from an orchestrator like n8n.
- The earlier n8n decision should be explicitly superseded in the [data-collection-stack map](../../data-collection-stack/map.md) so it stops being cited: it was made before multi-site reality and before the egress constraint existed.
- supercronic/docker-compose stays as-is for VM-side duties (pull-batch, discovery, api) — it is not the collection scheduler and shouldn't grow into one.

Draft implementation slice for the automation ticket (08): convert `collect.yml` to a per-retailer matrix, give retailers their own cron lines if intervals diverge, add the staleness watchdog + notification channel, and record the n8n supersession.

### 4. Questions only Feilim can answer

1. **GitHub Actions minutes budget**: is this repo public (unlimited free minutes) or private? At ~5 min per 4h run, five retailers ≈ 900–1,100 min/month — fine either way, but the answer decides whether per-retailer intervals can be aggressive or must be economized. Related: should **discovery** also move to CI (ticket 11 flagged the same IP-block risk for its endpoints), which multiplies minutes?
2. **Where do alerts land?** GitHub failure emails only? Or a real channel — ntfy/Telegram/Slack/Uptime-Kuma — and does the VM-side staleness watchdog ("freshest Tesco observation > 12h old") page the same place? This is the one piece that doesn't exist at all today.
3. **What freshness does each retailer actually need?** Same 4h cadence for all five, or e.g. Tesco 4h / Lidl+Aldi daily? (Lidl's thin online Drinks category may not reward 4-hourly polling, and fewer runs is also cheaper on Akamai's radar.)
4. **Formal close-out of the n8n pick**: happy for me to annotate the data-collection-stack map + ticket 06 as superseded-by-06-here, and archive `.scratch/data-collection-stack/prototype-n8n/`?

### 2026-08-30 — Feilim's rulings (agent)

1. **Repo is public** → GitHub Actions minutes effectively unlimited; no
   cadence economizing needed.
2. **Cadence: each retailer scraped once daily** (Feilim's explicit choice,
   simpler than per-retailer tiers).
3. **n8n: formally closed out** — superseded by this ticket's CI pick; keep the
   prototype in mind as a future tool (annotated in data-collection-stack map;
   prototype dir retained, not archived/deleted).
4. **Alerts: NONE (hobby project).** Feilim doesn't want notification spam.
   No alerting/watchdog will be built. Freshness and run status are checked
   passively via the operator dashboard / feed staleness when Feilim looks.
   GitHub's default failure emails land in the repo owner's inbox anyway and
   can be ignored or filtered — nothing further to set up.

Resolved: GH Actions scheduler, public repo, daily cadence, no alerting,
n8n closed.
