# Grind coverage to the strict bar

Type: task
Status: open
Blocked by: 05

## Question

Run the pipeline loop — improved matching, dashboard review sprints, collection runs over newly approved mappings, re-discovery for gaps — expanding mappings and observations until every one of the 100 catalog packs either has a current Price Observation at every viable retailer or holds an explicit exclusion. Track against the bar in `python -m beverage_feed report`; fold any retailer admitted by tickets 02/03 into the grind when they land. This is the long middle of the effort: expect multiple sessions, sprint-sized review batches, and honest `not_found`/exclusion outcomes rather than forced mappings.

Note: blocked-by on this file does not list 02/03 because grinding can start with Dunnes + SuperValu + Tesco; widen scope as research resolves.

### 2026-08-30 — prep (agent): gap list generated, ticket now unblocked

Ticket 17 lifted the real-data blocker. Gap list generated offline (zero
retailer requests) from `data/feed.sqlite`:

- `research/coverage-gaps-2026-08-30.md` — human summary + CI priority order.
- `research/coverage-gap-cells-2026-08-30.json` — the 69 open cells with
  candidate/evidence counts (via `discovery --list-targets` + SQLite enrich).

Headline: 23/500 approved; 69 open targets (29 dunnes zero-candidate, 40
sprint-ready); 408 untouched cells need first discovery (supervalu 90,
tesco 92, lidl 89, aldi 85, dunnes 52). CI egress runs should start with
first-discovery on untouched cells, then term-expansion on the dunnes 29.

Execution plan stays: CI first-discovery → term expansion → re-classify →
sprints (05) with Feilim's verdicts.

### 2026-08-30 — execution attempt (agent): both egress paths blocked; classification refreshed

**Item 1 — CI first-discovery on the 408 untouched: NOT EXECUTABLE.**

- `gh auth` is fine (workflow scope), but GitHub `workflow_dispatch` can only
  trigger workflow files present on the remote default branch, and the only
  discovery-egress workflow (`rediscover-tesco.yml`, commit `244a6e9`) sits
  among **31 local commits not pushed** to `origin/main` (agent is forbidden
  to push). `collect.yml` on the remote is *collection* (mappings →
  observations) and cannot discover unmapped cells.
- There is **no CI discovery workflow at all** for dunnes/supervalu/lidl/aldi
  — only the tesco one — and inventing one is out of scope.
- Even dispatchable, `rediscover-tesco.yml` needs a release snapshot
  (`state_release_tag`) to seed the CI database — **no releases exist** on the
  remote, and a fresh CI DB yields zero targets.

**Item 2 — dunnes term expansion: BLOCKED, new blocker.**

- Local run (permitted for dunnes per handoff; documented CLI,
  `discovery --rediscover --retailer dunnes --request-cap 200`, run
  `524b7b14`) failed on its **first outbound request: Dunnes HTTP 403**
  (diagnostic 2026-08-30T13:25Z). The failure-pause policy stopped the run
  after ~1 request; no decisions or data files were mutated.
- **Dunnes is now IP-blocking the operator machine too** (worked 08-24;
  connection resets 08-23; 403 today). Do **not** probe from home — same
  treatment as Tesco: dunnes discovery/search egress must move to CI.
- Consequence: the only viable egress for ALL discovery is CI, which needs
  (a) the local commits pushed, and (b) a generalized (or per-retailer)
  discovery workflow + a feed.sqlite release snapshot for seeding.

**Item 3 — re-classification of the 40 candidate-bearing targets: DONE
(local, zero egress, `review classify`).**

- Class counts for the 40 (dunnes 14, lidl 11, aldi 15):
  **A=2, B=34, C=2, unclassified=2** — dunnes A=2/B=8/C=2/unclassified=2;
  lidl all B (11); aldi all B (15).
- Whole-catalog classification (unchanged vs. this morning's baseline):
  candidate_cells 871 → A=5, B=416, C=5, D=6, excluded=439; cells 93 →
  A=2, B=39, C=3, D=0, unclassified=49; spot_check=1; rerun_targets=31 thin.
- The A=2 (dunnes `coca-diet-2000` + one more) are batch-approve sprint food
  (10% spot-check); the 34 B-cells are per-listing review work — dashboard
  sprints (ff-05) can be scheduled on these now.

**Needed from Feilim:** push authorization (or a push) for the 31 local
commits so `rediscover-tesco.yml` becomes dispatchable; a ruling on moving
dunnes (and likely all non-tesco discovery) to CI egress; whether to cut a
feed.sqlite release snapshot for CI seeding. Scheduler note: `collect.yml`
cron is still 4-hourly vs. the once-daily ruling (ff-06 implementation slice).

### 2026-09-03 — CI egress unblocked + first agent sprint (A/B/C cells)

- `rediscover.yml` replaces `rediscover-tesco.yml`: per-retailer dispatch
  (choice input), seeds from a release snapshot, uploads the partial DB
  regardless of outcome. `collect.yml` moved to once-daily (ff-06 ruling).
- Snapshot cut: release `feed-snapshot-2026-09-03` (6.4MB feed.sqlite,
  23 approved mappings + full discovery state).
- Agent sprint (three parallel worker subagents, strict rules, honest
  verdicts) over all 40 candidate-bearing cells / 815 candidates:
  **5 approved** (dunnes: coca-diet-330-single, coca-diet-500,
  coca-diet-2000, pepsi-original-330, 7up-330*), **809 rejected**,
  1 uncertain. All lidl (11) and aldi (15) cells: zero genuine matches —
  keyword noise only; those need CI discovery runs, not more review.
- One contradiction left for a Feilim sprint: `7up-330` candidate
  `dunnes:100297925:100297925` ("7UP … Can 12 x 330ml") got both approve and
  reject verdicts; latest evidence name says multipack → left rejected.
- Fixed `discovery_rejections` UNIQUE (canonical_key, rejected_at)
  collisions: same candidate across cells rejected in one second. INSERT OR
  IGNORE in `reject_candidate`, `inherit_rejection`,
  `reconcile_json_decisions` (rejection is per candidate identity).
- Next: dispatch `rediscover.yml` per retailer (aldi/lidl first — 0% cell
  evidence), ingest artifacts, repeat sprints. Cell states now: approved 23,
  rejected 40, inconclusive 28, pending 1.
