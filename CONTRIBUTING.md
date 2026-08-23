# Standards & Best Practices

Reference for agents working in this repo. If a rule here contradicts an ADR,
an issue spec, or a `CONTEXT.md` term, follow the more specific source and
surface the conflict explicitly.

Companion docs: `AGENTS.md` (agent skills overview), `docs/agents/domain.md`
(how to read domain docs), `CONTEXT.md` (the glossary),
`docs/agents/issue-tracker.md`, `docs/agents/triage-labels.md`.

---

## 1. Environment

- Python `>=3.11`, run from the project-local `.venv`.
- Install pinned dev checks with `.venv/bin/python -m pip install -e '.[dev]'`.
- Never install packages into system Python. Use the project-local virtualenv.
- The full local check suite, run before finishing any change:

  ```sh
  .venv/bin/python -m pytest          # tests (fails if none discovered)
  .venv/bin/python -m compileall -q beverage_feed
  .venv/bin/python -m beverage_feed --help   # CLI entry works
  .venv/bin/python -m mypy            # static typing
  ```

  CI (`.github/workflows/ci.yml`) runs exactly these steps. `pyproject.toml`
  is the single source of truth for tool config: `testpaths = ["tests"]`,
  `addopts = ["--strict-config"]`, and strict mypy flags.

---

## 2. Module layout

- Production code lives in the `beverage_feed/` package. Top-level modules:
  - `collector.py` — collection seams, retailer clients (Dunnes/SuperValu/Tesco),
    SQLite schema, feed/history/retention queries, the collection CLI `main`.
  - `matching.py` — catalog matching / normalization rules.
  - `discovery.py` — durable discovery state (store, schema, JSON decisions).
  - `discovery_adapters.py`, `discovery_decisions.py`, `discovery_run.py`,
    `discovery_report.py`, `discovery_cli.py` — discovery pipeline.
- `__main__.py` dispatches subcommands (`discovery`, `review`, `report`, else
  collection) to each module's `main(argv) -> int`.
- `__init__.py` re-exports the public API: `from .x import (...)`, then an
  explicit `__all__`. Add public names there; keep internals module-private.
- Throwaway exploration scripts (e.g. `camoufox_tesco.py`, `supervalu_*.py`,
  `find_api*.py`) live at the repo root **only** as gitignored prototypes, never
  inside `beverage_feed/`. Prefer the project-local `beverage_feed/` package for
  anything that becomes an actual tool (see `prototype` skill for the process).

---

## 3. Code conventions

- Every module starts with `from __future__ import annotations` and a module
  docstring. Public classes, functions, and non-obvious internals get docstrings.
- **Fully type-annotate everything** — mypy runs with
  `disallow_untyped_defs`, `check_untyped_defs`, `no_implicit_optional`. Use
  `str | None` (never `Optional[str]`-only in these files), `Mapping[str, Any]`,
  `dict[str, Any]`. Fix real typing errors; don't reach for `# type: ignore`
  (mypy `warn_unused_ignores`).
- Private helpers are `_snake_case`-prefixed and underscore-private. The public
  seam is what `__init__.py` exports; tests may import private helpers only from
  within the package when an integration is genuinely needed.
- One conceptual aperture per module. `collector.py` is deliberately large but
  cohesive: adapters are thin clients that feed shared, retailer-neutral
  persistence. Prefer adding a focused new module over growing a grab-bag.
- Imports: standard library first, then local package modules. Sort groups
  alphabetically within each block.
- Constants that vary per retailer (endpoints, keys) are module-level
  `UPPER_SNAKE` constants so a source change is one edit away from tests.

---

## 4. Money

This domain is money; never use `float` for prices, deposits, or per-unit values.

- Prices are `Decimal`, parsed with `_decimal_price` and serialized with
  `_decimal_text`. Rounding is `ROUND_HALF_UP`.
- Per-unit values are **derived**, never stored from the source:
  - `component_unit_price = displayed_price / pack_count`
  - `price_per_litre = displayed_price / litres` (quantized `0.0001`)
- Displayed Price, Clubcard Price, and DRS Deposit are recorded **separately**
  (see glossary) and stored as text columns, not summed.
- Malformed prices must not throw in the middle of an observation; they demote
  the result to `source_error` with the raw record kept for diagnostics.

---

## 5. Time

- All timestamps are UTC, ISO-8601 with a `Z` suffix, second precision, produced
  by the shared `timestamp()` helper
  (`datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00","Z")`).
- Use the existing helper rather than re-deriving the format. Never store naive
  local time. Rehydrate with `as_datetime` (which tolerates `Z` and naive input).

---

## 6. SQLite

- Persistence is SQLite. Every module that touches a live database opens it
  inside `with closing(sqlite3.connect(...))` and calls `ensure_schema(connection)`
  (or the equivalent discovery schema) before use.
- `PRAGMA foreign_keys = ON` is set in `ensure_schema`. Existing schemas are
  migrated forward with idempotent `PRAGMA table_info` + `ALTER TABLE` additions,
  never destructive drops — a database must stay usable across milestones.
- **Always use parameterized queries (`?` placeholders)**. Never interpolate
  values into SQL strings.
- Writes within a transaction commit explicitly (`connection.commit()`).
- This is an **append-only observation store** where it matters: a successful
  observation is an inserted row; a failed run never deletes or overwrites one.
  Absence of an observation is not an inventory claim.
- Secrets in raw records are scrubbed before persistence — use `safe_record`,
  which redacts keys matching `authorization|cookie|password|secret|token|api.?key`.

---

## 7. Domain vocabulary and responsibilities

- Use glossary terms exactly as defined in `CONTEXT.md`. Don't rename a concept
  to a synonym the glossary avoids (see `docs/agents/domain.md`).
- **Collection owns Price Observations.** `collector.py` is the sole writer of
  observations. Discovery records evidence and mapping decisions only and never
  creates a Price Observation.
- Mapped/available is not the same as observed. Collection result statuses are:
  `observed` (writes a Price Observation), `not_found` (no matching listing —
  distinct from an outage), `source_error` (request/parse failure), `unmapped`
  (no approved mapping). Only `observed` reaches the Current Feed.
- The Current Feed presents the latest result per retailer-pack pair; an older
  price must not be shown as current even when the newest result is
  `not_found`/`source_error`.

---

## 8. Error handling

- Distinguish failure kinds deliberately:
  - Validation of inputs/arguments → `ValueError` (often with an explanatory
    message), tested via `assertRaises(ValueError)`.
  - No matching product → `LookupError` mapped to `not_found`.
  - Anything unexpected from a live source → `RuntimeError` mapped to
    `source_error`, chained with `raise ... from exc`.
- A failing retailer-pack pair must not break the rest of the run; per-pair
  results are isolated and aggregated in the run summary.
- Retries are bounded (`max_retries`), with exponential backoff and a per-request
  spacing where the source rate-limits. Respect `min_request_interval`.

---

## 9. CLI

- Each CLI is an `argparse` parser in a `main(argv: list[str] | None = None) -> int`
  function. `main` returns an exit code: `0` for a completed/successful run,
  nonzero otherwise. Never call `sys.exit` from inside the package.
- Defaults point at repo files (`data/catalog.json`, `data/mappings.json`,
  `data/feed.sqlite`). Secrets/required values read from environment
  (`TESCO_API_KEY`, `SUPERVALU_STORE_ID`) rather than being hardcoded.
- Operator-facing output is a compact single-line summary; verbose detail goes
  to diagnostics tables.

---

## 10. Data files

- `data/` holds the curated inputs: `catalog.json` (Benchmark Catalog packs),
  `mappings.json` (retailer→catalog mappings), `rejections.json` (review decisions).
- `data/*.json` and `*.sqlite` are committed where present (catalog/mappings are
  versioned input); **throwaway JSON/HTML dumps and `storage/` (Crawlee state)
  are gitignored**. Never commit credentials or `.env`.

---

## 11. Tests

- Tests live in `tests/`, discovered by pytest from `testpaths = ["tests"]`.
- Style: `unittest.TestCase` subclasses named `XxxTests` with `test_*` methods;
  fixtures built in `setUp`. One test file per module under test, mirroring the
  package name (`tests/test_collection.py` ↔ `beverage_feed/collector.py`).
- **Tests never call live retailer endpoints.** They use captured fixtures and
  test doubles (fake fetchers/adapter objects). Keep them fast and hermetic —
  use `tempfile.TemporaryDirectory()` for scratch SQLite/JSON paths.
- The suite **fails when no tests are discovered** (guards against an empty run
  passing).
- Test names read as behavior: `test_..._uses_..._without_...`, asserting
  outcomes (observed/not_found/source_error), not SQL internals.
- Write a test that pins the behavior you rely on before relying on it (see
  `tdd` skill). When a rule here changes, change the test that pins it.

---

## 12. Issues, ADRs, and history

- Track work as markdown under `.scratch/<feature-slug>/` per
  `docs/agents/issue-tracker.md`; record triage `Status:` roles per
  `docs/agents/triage-labels.md`. `.scratch/` and `docs/` are local-only
  and gitignored.
- Prefer committing focused, working increments. Root-level throwaway scripts
  exist to document research but are gitignored once superseded.
- Preserve the JSON decision files' stable formatting (`indent=2, sort_keys=True`
  plus trailing newline) — `discovery.py` writes them atomically and validates
  on load.
