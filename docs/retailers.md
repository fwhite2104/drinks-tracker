# Retailers

All five Tier-1 retailers are admitted; automated discovery/collection
currently covers **Dunnes, SuperValu, Tesco**. **Lidl and Aldi adapters are
built and tested but deferred from automated runs** (2026-09-04 scope
amendment in `goal.md`: brand-term search there returns only keyword noise;
no category walk shipped). Re-admit via explicit `--retailer` runs.

**All retailer egress happens on GitHub Actions** (rotating cloud IPs). The
home network is IP-blocked — Akamai blocks Tesco across all of tesco.ie
(2026-08-27), and Dunnes 403'd the home IP (2026-08-30). Never probe a
retailer from home; see [`operations.md`](operations.md).

Access routes were validated in `.scratch/full-feed-coverage/research/`
(gitignored local captures) and the per-retailer tickets.

## Dunnes Stores — `dunnes.py` seam in `collector.py`

- **Route**: the grocery site (dunnesstoresgrocery.com) exposes a JSON search
  API on a separate `storefrontgateway` host, not Cloudflare-gated. Results
  are translated into the `productSearch.products` VTEX-style envelope.
- **DRS deposit** extracted from offer evidence / `taxDetails`.
- **Search relevance is exact-substring**: full pack names often return 0
  ("Coca-Cola Original Taste 1.5L Bottle" → []), while bare brand terms
  return the whole clean range ("Coca-Cola" → 39 products). Discovery
  searches brand + term + aliases (deduped) and collection falls back to the
  brand term after a provably complete page; the staleness guard checks
  brand tokens only.
- **Known honest exclusion**: Dunnes does not stock Coca-Cola 1.5L
  (1L/2L only).

## SuperValu — `finders.py` + `collect_supervalu_one`

- **Route**: storefront gateway REST, store-scoped via `SUPERVALU_STORE_ID`
  (a store secret; store-scoped pricing is the observed granularity).
- **Mapping identity**: `source_product_id` (older `source_item_id` /
  `source_product_reference` fields were folded into it by the ff-17
  schema migration).
- **DRS deposit** extracted per listing record.

## Tesco Ireland — `collect_tesco_one` in `collector.py`

- **Route**: `search.api.tesco.com/search` (public search API) +
  `xapi.tesco.com/` GraphQL, authenticated with `TESCO_API_KEY`.
- **Clubcard price**: tagged via the `CLUBCARD_PRICING` attribute and phrased
  "€X Clubcard Price"; extracted alongside the public price, never instead
  of it.
- **DRS deposit**: from the charges fragment.
- **Blocker**: Akamai IP-blocked from the home network across all of
  tesco.ie (worked with plain urllib 2026-08-26; the 4-hourly cron likely
  tripped it) — the event that spawned CI egress.
- Current coverage gap is discovery (rate window), not verdicts.

## Lidl Ireland — `lidl.py`

- **Route**: undocumented plain REST, no auth, no bot protection:
  - `{base}/q/api/search` — required params `q`, `locale=en_IE` (underscore),
    `assortment=IE` (case-sensitive), `version=2.1.0` (**load-bearing**;
    omitting it yields an opaque Spring 400), plus `fetchsize`/`offset`.
  - `{base}/p/api/detail/{productId}/IE/en` — detail: EANs, price block,
    stock badges.
- **Structural limitation**: pack size exists **nowhere** on lidl.ie
  (listing payload, detail API, schema.org all lack it). Titles are the only
  signal; `parse_title_pack` parses conservatively — no size in the title
  means no invented pack evidence.
- **Assortment**: online Drinks category is thin (~8 items); real soft-drink
  breadth unverified.

## Aldi Ireland — `aldi.py`

- **Route**: Spryker Glue JSON API at `asl.api.aldi.ie/commerce` (behind
  Azure APIM; no auth). The public web edge sits behind Akamai, but that is
  only a header-signature check — the catalog API itself is open:
  - `{base}/v3/product-search?q=...&limit=..&offset=..` — paginated bulk
    search, fully priced products
  - `{base}/v2/products?skus=SKU1,SKU2` — priced batch lookup
  - `{base}/products/{sku}?servicePoint=D001&serviceType=walk-in` — priced
    single-SKU lookup (`servicePoint` required)
- **Money**: integer euro cents + display strings end to end — no float
  conversion ever.
- **Pack size**: `sellingSize` strings (`"1 L"`, `"330 ML"`, `"6 x 330 ml"`)
  parsed to `(pack_count, unit_size_ml)`; non-volume units (`"6 Each"`)
  return `None` rather than inventing a size.

## Shared behaviour

- **Matching**: every returned listing is checked against the exact-pack bar
  (brand, variant, pack count, unit size, package type) after the curated
  Brand Alias translation ([`discovery-and-review.md`](discovery-and-review.md)).
- **Resilience**: `source_http.py` classifies transport failures, 429s and
  retryable 5xx once; collection retries with bounded backoff + jitter,
  honours `Retry-After`, spaces requests per retailer, and opens a circuit
  breaker after repeated failures. Thin clients (`lidl.py`, `aldi.py`) keep
  their own transports and are documented adopters of this module.
- **Known incompleteness is modelled, never guessed**: a page that is
  truncated or of unknown completeness cannot produce a `not_found` verdict —
  only a proven-complete page can.
