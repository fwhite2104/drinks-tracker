# Discovery & Review

How unmapped catalog cells get mappings — and how the operator's decisions
become durable state. Vocabulary: [`../CONTEXT.md`](../CONTEXT.md).

## The strict bar

A Catalog Mapping is valid only when the listing matches the pack on **all
five exact-pack attributes**: brand, variant, pack count, unit size, package
type. The bar is never lowered for speed. A cell is "done" only when it has
an approved mapping + observations, an explicit exclusion, or an honest
inconclusive — nothing silently missing.

## Discovery runs

A **Discovery Run** is a bounded evaluation of retailer searches for active,
not-yet-mapped cells (`discovery_run.py`). It records:

- what was searched (search terms, request events with batch sizes and
  request accounting — `bootstrap` / `search` / `pagination` / `hydration`),
- what evidence came back (normalized listings in
  `discovery_candidate_cells` + `discovery_candidate_evidence`),

and **creates no observations and makes no decisions**. Budgets cap requests
per run and per search; failure-pause policy stops a run on repeat failures
rather than hammering a blocking retailer.

**Term expansion** (ff-14): before a human ever reviews a cell, cheap search
alternatives are tried first — `matching.search_formulations(pack)` generates
the ordered per-pack alternates (search term → curated aliases →
count-explicit → size-explicit, max 4), and rediscovery targets only
thin/Class-D/pending cells. Decided cells are never re-searched.

## Translation layer (the fix for "Diet Coke ≠ Coca-Cola Diet")

- Curated **Brand Alias** dictionary (`matching.py`) + per-pack aliases in
  `catalog.json`; longest-phrase-first, case/hyphen insensitive. Applied at
  extraction time to rewrite listing identity to the canonical brand/variant
  — *before* the exact bar is applied. It never weakens the bar.
- Auto-mined aliases stay **suggestions** until a review sprint edits the
  table. A guard prevents alias translation from widening across variants
  (e.g. dragging "Zero" into a Diet cell).
- A **junk relevance gate** (`is_relevant_candidate`) sets obviously
  irrelevant rows (clothing, lamps, sweets) aside as `excluded` during
  classification — they are never classified and never reach review.

## Evidence classes A–D

`discovery_classify.py` re-runs the matcher over persisted evidence
(`review classify`) and classifies each candidate-cell:

- **A** — clean: batch-approve with a deterministic ⌈10%⌉ spot-check; any
  surprise demotes the batch to per-item.
- **B** — name disagreement: per-item review.
- **C** — ambiguous (multi-candidate, conflicting clean names): per-item.
- **D** — price missing / thin: defer to a re-run (ff-14 rediscovery).

Cell rollup: any C → C; clean-only → A; else B; non-decided D plus thin
cells form `rerun_targets`. `batches` + `spot_check` are what the sprint
queue serves, A-first.

## Review sprints (dashboard, not terminal)

Terminal review of large batches is explicitly rejected (standing operator
preference). Review happens in dashboard sprints:

- `run_dashboard.py --sprint` — keyboard queue, class-A-first; `a` approve
  (auto-replaces on an approved cell via `replace_mapping`), `r` reject,
  `x` exclude, `s`/`A` batch, shift+j/k range select, challenge flow; sidebar
  rubric: approve = all five attributes incl. variant; exclude = an
  assortment claim, **not** evidence absence (no candidates ≠ not stocked).
- **Agent sprints** apply verdicts through the real discovery CLI seam only
  (`discovery_cli.py` — no parallel decision logic) with evidence-citing
  reasons and `decided_by=agent-sprint`; the operator spot-checks.
- Sprint verdicts recorded 2026-08-30 (ff-05): queue order A-first,
  side-by-side fine, shift-range select, approve auto-replaces, 20-cell pass
  fits 15 minutes.

## Durable decisions

Decisions are **JSON-first**: they land in `mappings.json` /
`rejections.json` and are reconciled into SQLite by `reconcile_json_decisions`
with guards:

- a listing rejection of a *competing* candidate never demotes a cell that
  has an approved mapping;
- rejections are keyed per candidate identity (INSERT OR IGNORE guards
  same-second collisions);
- **operator approval outranks search provenance** when they conflict.

Exclusions are **Cell Exclusions** (retailer×pack) and require a confirming
re-run before being recorded — an absence claim must be provable.

## Known gaps (as of 2026-09-06)

- **ff-15 — global not-a-beverage rejection**: rejections are cell-scoped, so
  one junk product must be rejected once per cell it pollutes. A
  retailer-wide reject scope (canonical_key × retailer) is specified but not
  yet implemented.
- **ff-16 — catalog variant expansion**: real sibling variants (7UP Sugar
  Free & co.) surfaced by discovery should become catalog cells instead of
  being re-rejected per cell. Decided by the operator; not yet implemented.
- **Catalog size vs comparability**: only 19/100 packs have ≥2-retailer
  evidence. Open question (see `status.md`): shrink the catalog to the
  comparable core, or grind discovery for the rest?
