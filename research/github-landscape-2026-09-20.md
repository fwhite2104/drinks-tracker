# GitHub landscape & peer practices — price discovery/logging (2026-09-20)

Read-only research. No retailer egress; every claim below is from `gh` searches,
`gh api` repo metadata, public APIs, or the Apify store, all probed 2026-09-20
(star counts are that day's snapshot). Extends, and does not repeat,
`04-tools-and-skills-recommendations.md` (2026-08-13 tools list),
`reverse-engineering-trolley.co.uk.md` (2026-09-06 acquisition model) and
`smartcart-scraper-spike-2026-09-20.md` (per-retailer techniques).

Two questions answered:
1. What exists on GitHub we can use to improve this repo?
2. How do other people actually discover and log grocery prices?

---

## TL;DR — the seven findings that matter

1. **Our GTIN machinery is built but switched off.** `gtin_matches()` is
   three-valued and correct (`matching.py:243`), `BenchmarkPack.gtin` exists
   (`collector.py:78`), the Tesco GraphQL query already requests `gtin`
   (`collector.py:978`), and `lidl.py` already fills listing `gtin` from
   `gridbox.meta.ean` / detail `eans[0]`. But **zero** entries in
   `data/catalog.json` and `data/mappings.json` carry a GTIN — so `pack.gtin`
   is always `None`, `gtin_matches` always returns `None`, and the strongest
   identity evidence we have is dormant. Backfilling GTINs is the single
   highest-value change found in this scan.
2. **Third-party Irish price sources exist beyond BasketWatch.** Apify has
   `hawkish_gym/basketwatch-irish-grocery-data` (already researched),
   `barefoot_grade/dunnesstores-ie-scraper` (dunnesstores.com, fashion+grocery
   mixed, $0.40/1k rows), and `blackfalcondata/aldi-scraper` (14 countries
   incl. Ireland, shelf + unit price + pack size). Paid, third-party, ToS
   caveats — but usable as cross-check or gap-fill without new adapters.
3. **A direct consumer competitor shipped in Ireland: Cisean** (iOS app,
   Dec 2025: scan barcode → compare prices across Irish supermarkets). Its
   companion `cisean/barcode_database` (MIT) publishes Irish GTIN → product
   data as CSV/SQL release assets — 1,205 rows, exports stalled after
   2026-01-07. Market signal + a free GTIN seed for our catalog.
4. **Open Prices (Open Food Facts) is not an Irish source — today.** Measured
   live: Ireland has 24 locations / 50 prices vs France 2,491 / 209,561; a
   sampled Irish barcode returned 0 rows. Keep it as a GTIN identity oracle
   (we already use OFF that way) and as the reference model for open-data
   publication, not as a feed.
5. **A permissively-licensed reference implementation for our exact five
   retailers exists**: `but3k4/supermarket-mcp` (MIT) — curl_cffi TLS
   impersonation + FlareSolverr fallback, `__PRELOADED_STATE__` parsing for
   Dunnes/SuperValu, `api.aldi.ie` JSON for Aldi. MIT means we may read and
   adapt code, unlike the unlicensed SmartCart.
6. **`D4Vinci/Scrapling`** (BSD-3-Clause, 82.6k★, active daily) is the
   strongest new tool for our weakest surface: its parser *auto-relocates
   elements after a site redesign* and its fetchers are stealth-grade. That is
   the Lidl `/s{id}` JS-page and layout-churn problem, in one library.
7. **Two cheap ops gaps closable with existing projects**: `healthchecks`
   (BSD-3, 10.3k★) for a dead-man's switch on the CI collection + VM
   pull-batch (our "no alerting" decision becomes "no alerting unless the
   pipeline dies"), and `changedetection.io` (Apache-2.0, 34.4k★, has
   first-class price/restock tracking) for ad-hoc watching of a handful of
   listings without writing an adapter.

---

## A. What's on GitHub we can use

### A1. Identity & matching (highest leverage for us)

| Project | License / activity | What it changes for us |
|---|---|---|
| GTIN backfill (no dependency — our own seam) | — | `catalog.json` gains `gtin`; approved mappings record the GTIN observed at approval. `gtin_matches` then upgrades discovery verdicts from name-judged to barcode-proved, and collection can fail a drifted listing instantly. Directly attacks the wrong-product class (`research/wrong-product-audit-2026-09-20.md`) and the ~12 ambiguous + ~35 pending ff-07 verdicts. |
| `arthurdejong/python-stdnum` | LGPL-2.1 · 594★ · pushed 2026-08-15 | Validate EAN-13/GTIN check digits on ingest (cheap guard: retailer payloads sometimes carry internal codes that look like EANs). LGPL is fine for an unmodified dependency. |
| `cisean/barcode_database` | MIT · 19★ · stale since Jan 2026 | 1,205 Irish GTIN → product_name/brand/package_size rows (CSV + SQL release assets) as a *seed* for catalog GTINs; verify each against the retailer listing before use (it is crowdsourced). |
| Open Food Facts API (`openfoodfacts/openfoodfacts-python`, 466★) | AGPL data (ODbL), SDK active | GTIN → canonical brand/name/quantity, used ad hoc already (Monster Ultra White GTIN confirmed via OFF, commit `df8c8bf`). Formalise as a discovery evidence source, not a price source. |
| `rapidfuzz/RapidFuzz` | MIT · 4.1k★ · active | Fuzzy name pre-ranking (SmartCart uses `token_sort_ratio ≥ 85` + unit cross-check). Additive only: rank candidates for the operator, never approve. Their `matching.py` is regex/exact today. |
| `scrapinghub/price-parser` | BSD-3 · 348★ · pushed 2026-08-06 | Robust parsing of promo strings ("2 for €3", "Any 3 for £3" artifacts noted on feed rows). Only if promo text parsing grows. |
| `scrapinghub/extruct` | BSD-3 · 972★ | JSON-LD/microdata extraction — relevant only if a category-walk page (Lidl/Aldi) lacks a JSON API and embeds schema.org `Product`/`Offer`. |

### A2. Acquisition & anti-bot

| Project | License / activity | What it changes for us |
|---|---|---|
| `D4Vinci/Scrapling` | BSD-3 · 82.6k★ · pushed 2026-09-19 | Adaptive parser (`p.css('.x', adaptive=True)` relocates after redesign) + stealth fetchers + spiders with pause/resume. Best candidate for Lidl `/s{id}` (JS-rendered range pages) and for any future DOM-based walk. Heavy dep (browser install) — CI-only, like collection. |
| `but3k4/supermarket-mcp` | MIT · 0★ · Jun 2026 | Reference implementation for our five retailers: shared curl_cffi fingerprint, FlareSolverr cookie-reuse for Dunnes/SuperValu JS challenges, `__PRELOADED_STATE__` promo parsing, Aldi over `api.aldi.ie`. Read its `stores/` modules before our next Lidl/Aldi/Tesco transport change. |
| `FlareSolverr/FlareSolverr` | MIT · 15.7k★ · active | Solves Cloudflare-class challenges (cookie reuse). **Does not solve Akamai** (Tesco) — our CI-egress decision stands; FlareSolverr is a fallback for a Cloudflare-gated surface only. |
| `Kaliiiiiiiiii-Vinyzu/patchright` | Apache-2.0 · 4.6k★ | Drop-in undetected Playwright. Consider only if a JS-rendered surface becomes load-bearing and Scrapling's fetchers are not enough. |
| `ultrafunkamsterdam/nodriver` | **AGPL-3.0** · 4.8k★ | Best-in-bench stealth in an independent 2026 comparison, but AGPL on a long-lived service is a real obligation. Note before adopting. |
| `daijro/camoufox` | MPL-2.0 · 12.0k★ | The 2026-08 recommendation. Defer: CI egress already solved the problem it was bought for. |
| `pim97/anti-detect-browser-tools-tech-comparison` | docs repo | Honest side-by-side (lineage, code quality, licensing) — read before adopting any stealth tool. |

### A3. Ops & publishing

| Project | License / activity | What it changes for us |
|---|---|---|
| `healthchecks/healthchecks` | BSD-3 · 10.3k★ | Dead-man's switch: `collect.yml` pings on success, VM `pull-batch` pings on ingest; silence = alert. Closes the "no alerting" gap (ff-06) without building an alerting system. One container. |
| `dgtlmoon/changedetection.io` | Apache-2.0 · 34.4k★ · active | Self-hosted price/restock watcher with visual selectors and price-change rules. Use as a *supplementary* watcher for a handful of high-value listings (e.g. a Lidl campaign page) — not a pipeline replacement; it has no exact-pack identity model. |
| `simonw/datasette` | Apache-2.0 · 11.5k★ | Read-only exploration/publishing over `feed.sqlite` (SQL + JSON API). Optional QA surface for research joins; our FastAPI feed stays the product API. |
| JustPaddy/grocery-price-data | no license · 0★ · last release Jul 2026 | Pattern, not code: unified Irish price data (Tesco, Dunnes, SuperValu, Aldi) published as **SQLite via GitHub Releases** for an app to sync. Same-market peer doing exactly our job; a fallback delivery shape for the mobile feed if the tunnel (ff ticket 10) proves fragile. |
| Our own repo settings | — | `fwhite2104/drinks-tracker` has 0 open issues/PRs, but **Dependabot alerts are disabled**. Enabling alerts-only is a free CVE signal on the pinned deps (`curl-cffi`, `fastapi`, `httpx`) with no version churn. |

---

## B. How other people do it

### B1. Acquisition: four archetypes, and where we sit

| Archetype | Who | Mechanics | Implication for us |
|---|---|---|---|
| Product/affiliate feeds | Trolley.co.uk (~40 UK stores) | Retailers push daily SKU feeds (name, price, deep link, usually GTIN) to affiliate networks; ingest is authorised traffic, no WAF | Still the cheapest theoretical route; Irish coverage unverified (human signup step, already recommended 2026-09-06) |
| Purchased scraper datasets | BasketWatch Ireland (Apify, $3/1k rows, ~47k SKUs daily across our exact 4 retailers); barefoot_grade, blackfalcondata actors | Specialist runs the scraping + QA; you buy rows with `scrape_date` | Cheap cross-check/gap-fill; never the ground truth (third-party, resold) |
| Own scrapers | Us; SmartCart; supermarket-mcp; JustPaddy's pipeline | Per-retailer clients, search or category walk, own DB | Our model, and ours is the only one with exact-pack identity + honesty states |
| Crowdsourced | Open Prices (receipts/OCR), Cisean (barcode scans) | Users submit prices/barcodes; community DB | Precedent for open publication; Irish coverage too thin to consume today |

### B2. Discovery: search vs walk vs ingest

- **Search-first** (us): exact-substring relevance, brand terms, term expansion.
  Failure mode = the term you didn't think of (Tesco 8/100 is a search gap).
- **Category walk** (SmartCart; supermarket-mcp): enumerate the assortment by
  walking category pages — in-scope by construction, no search-term luck.
  Requires DOM/JS handling on Lidl `/s{id}` and Aldi `/products/drinks/`.
- **Full-catalog ingest + local classify** (Trolley, BasketWatch): pull
  everything, classify against the catalog locally (`discovery_classify`
  already does this shape). Turns discovery from "find my product" into
  "recognise my product in a stream" — strictly more powerful when a feed
  exists.
- Pattern to copy: **invert the pipeline where a feed exists, keep search as
  fallback.** Where no feed exists (our case), category walk is the next best
  enumerator; search stays as the fallback.

### B3. Identity: GTIN first, fuzzy second, human last

Converging industry practice (Trolley feeds, SmartCart's matcher, the
`studio-amba/uk-grocery-price-matrix` Apify actor — "matches products by EAN
barcode with a fuzzy-name fallback") is a strict order:

1. **Exact GTIN/EAN** — the same physical pack, provable.
2. **Normalized fuzzy name** (token-sort ≥ ~85) **plus a unit/pack-size
   cross-check** — SmartCart rejects fuzzy matches whose parsed size differs.
3. **Human verdict** — for genuine ambiguity.

We have all three layers, but layer 1 is inert because no GTINs are stored
(see TL;DR #1). The industry treats GTIN as the join key; our catalog treats
names as the join key and GTIN as an optional extra.

### B4. Logging & publication patterns

- **Append-only observations with an as-of date** — universal. BasketWatch
  puts `scrape_date` on every row; Open Prices timestamps proofs. Matches our
  Price Observation model.
- **Unit-price normalization** — universal (Trolley per-100g/ml, BasketWatch
  €/kg + €/L, Dutch `thijskuilman/open-supermarket-api`). We derive
  `price_per_litre` / `component_unit_price` already.
- **Promotions and loyalty prices stored beside, never instead** — BasketWatch
  ships a separate `promotions` dataset (offer text, was-price, validity
  window, Clubcard/Real Rewards/member prices). We do this per-observation
  (Clubcard separate, DRS separate).
- **Day-over-day change datasets** (`changes`, `new-products`, `removed`) —
  BasketWatch's most interesting shape: consumers of price data mostly want
  *deltas*, not snapshots. We have history but no "what changed today" view.
- **Publication**: API (us, Trolley's paid API), SQLite-via-GitHub-Releases
  (JustPaddy), open JSONL/Parquet dumps (Open Prices: `prices.jsonl.gz`,
  HuggingFace Parquet), crowdsourced frontends (Cisean, Open Prices).
- **Licensing is part of the product**: Open Prices data is ODbL with
  attribution; Cisean's DB is MIT. If we ever publish, the same choice
  arrives.

### B5. Ops patterns

- **Egress discipline** — we already lead: collection on GitHub Actions
  (rotating IPs), VM pulls batches, canary gate. supermarket-mcp's
  IP-bound-cookie warning ("run the MCP container on the same host as
  FlareSolverr or cookies get challenged") is the same lesson in miniature.
- **Change detection as a first-class concern** — changedetection.io exists
  precisely because scraping teams watch pages they can't parse; Scrapling's
  adaptive parser is the same idea inside the scraper.
- **Freshness/health signals** — every serious peer surfaces "when was this
  price seen"; we do (`scrape_date`, Last Seen, freshness module). The gap is
  pipeline-death detection, not price staleness.

### B6. What we already do that peers don't

Worth stating so recommendations don't drift toward the median:

- Exact-pack agreement bar with operator verdicts and evidence classes (A–D).
- Five honest consumer states; absence never claims stock or retirement.
- DRS deposit as its own line; Clubcard beside, never instead.
- A live canary + release gate before trusting a feed run.
- Composition guard at the shared seam after the 12-pack incident.

These are the reasons the feed can be trusted; none of the projects above
replicate them.

---

## C. Recommendations (ranked; effort = agent-hours, not calendar)

1. **Backfill GTINs into `data/catalog.json` and approved mappings** (S/M).
   Sources, in order: Tesco GraphQL `gtin` (already requested), Lidl detail
   `eans[0]` (already parsed), Aldi detail payloads, OFF for identity
   confirmation, Cisean CSV as a seed list. Validate with `python-stdnum`.
   Then let `gtin_matches` feed discovery verdicts (evidence class above A)
   and collection fail-fast. *Ties to: ff-07 review load, wrong-product
   hardening, ff-16 (new cells start GTIN-tagged).*
2. **Trial a BasketWatch pull as an independent cross-check** (S). $0.35/100
   rows; compare N=50 of our current observations against their rows for the
   same SKUs; report agreement/disagreement. Decides whether it is a QA
   signal, a gap-filler for single-retailer packs, or neither. *Ties to: ff-07,
   catalog-size question (their `changes` dataset shows what "the market"
   actually moves).*
3. **Category-walk probe for Lidl/Aldi, on CI egress** (M). This is the
   SmartCart spike's GO item, now with a tool: if `/s{id}` needs JS, Scrapling
   in CI is the least-effort path; capture one drinks category per retailer
   and count in-scope packs. *Ties to: Lidl/Aldi re-admission, ff-07.*
4. **Dead-man's switch on the pipeline** (S). Self-hosted healthchecks: ping
   from `collect.yml` on success and from VM `pull-batch` on ingest; alert on
   silence. *Ties to: ff-06 "no alerting" decision — this preserves it while
   removing the silent-death failure mode.*
5. **Optional: changedetection.io for a short watchlist** (S). A handful of
   listings we suspect drift (e.g. Lidl campaign pages, a Dunnes multipack
   listing) watched out-of-band; confirms drift before we build anything.
6. **Read `supermarket-mcp/stores/` before the next transport change** (S).
   MIT-licensed validation of Dunnes/SuperValu/Aldi/Lidl routes; adopt
   FlareSolverr only for Cloudflare-class surfaces, never for Tesco/Akamai.
7. **Do not adopt, and why** (decision hygiene):
   - Scrapy/Crawlee rewrite — our architecture (typed adapters + shared
     persistence + operator verdicts) is the product; a framework migration
     buys nothing the canary doesn't already protect.
   - Airbyte/n8n pipelines — scheduler decision (ff-06) is closed; Actions
     wins on cost and egress.
   - camoufox/patchright/nodriver — keep as contingency for a future
     JS-rendered surface; no current surface justifies the dependency
     (note nodriver's AGPL).
   - Open Prices as a price source — measured empty for Ireland; revisit if
     their IE coverage ever grows.

---

## D. Caveats

- **Licenses differ materially**: Scrapling BSD-3, changedetection Apache-2.0,
  healthchecks BSD-3, datasette Apache-2.0, supermarket-mcp MIT, Cisean DB
  MIT, python-stdnum LGPL-2.1, patchright Apache-2.0, **nodriver AGPL-3.0**,
  camoufox MPL-2.0, FlareSolverr MIT. Open Prices data is ODbL (attribution +
  share-alike obligations on derived open data).
- **Star counts / activity are a 2026-09-20 snapshot** and were read via
  `gh api`; re-check before adopting.
- **Apify actors are third-party, paid, and their scraping ToS posture is
  theirs, not ours.** Treat rows as untrusted evidence (matching still
  applies, never auto-approve) and verify currency/deposit handling on any
  Irish rows (the GBP-format artifact on tesco.ie rows is a known example).
- **Cisean's exports stalled** (last asset 2026-01-07) and its domain now
  serves an unrelated product; treat the DB as a static seed, not a feed.
- **Never probe retailers from this network** — all live verification of
  anything above runs on CI egress (Tesco Akamai-block, Dunnes 403).
- The supermarket-mcp repo is 0★ and one author's work; it is a *reference*,
  not a dependency — read the endpoints/selectors, verify against our own
  captures, then decide.

## E. Reproducing this scan

`gh search repos` queries run (limit 8–10 each, `--json`): grocery price
comparison · supermarket scraper · price tracker self-hosted · open prices ·
price monitoring · ireland grocery · ecommerce product matching ·
changedetection · anti-bot browser automation stealth · gtin barcode product ·
price history tracker · grocery dataset · instacart scraper · supermarket api ·
scrapy playwright · tesco scraper · dunnes stores · supervalu · irish
supermarket. Metadata via `gh api repos/<owner>/<repo>`. Open Prices probes:
`/api/v1/prices?product_code=<barcode>`, `/api/v1/locations/osm/countries`,
HuggingFace datasets-server (`openfoodfacts/open-prices`, 314,330 rows).
Apify store pages fetched directly. Prior in-repo research: see the three
files named at the top.
