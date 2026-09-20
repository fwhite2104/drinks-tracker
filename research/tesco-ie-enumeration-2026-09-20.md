# Tesco IE enumeration — dossier intake (2026-09-20)

Received from Feilim (Perplexity Pro research, 2026-09-20). This file keeps the
repo-side reading of it: what is new, what is already known, what is wrong, and
the exact next action. Raw claim set quoted where it matters; every item the
dossier itself flags UNVERIFIED stays flagged here.

## What it changes

**`browseCategory` exists on the storefront GraphQL gateway (`xapi.tesco.com`)
alongside `search`.** That is the capability behind the biggest coverage gap
today: Tesco holds 9 of 86 approved mappings and has 10 candidates in the whole
discovery store, so 23 packs sitting at "Dunnes + SuperValu, needs Tesco" have
no Tesco evidence because discovery only searches by term. A category walk over
Groceries > Drinks would enumerate the aisle instead of guessing formulations —
the same shape as the Lidl/Aldi `walk_drinks` seams already in
`beverage_feed/discovery_adapters.py`.

Supporting evidence the dossier cites (third-party, not Tesco docs):
- `basketeer` (npm/GitHub, 2026, MIT) documents anonymous `search`,
  `getProduct`, `browseCategory`, `nutrition` on `xapi.tesco.com` with a public
  `x-apikey` header, and states the bundled key rotates roughly monthly.
  Its author states **UK only, IE untested**.
- Apify actors (`radeance/tesco-scraper` 2024→current; `thenetaji/tesco-scraper`
  2025) advertise tesco.ie support; the latter exposes an explicit
  `"region": "IE"` input and tesco.ie product URLs (`/shop/en-GB/products/…`).
- No public source gives browseCategory page size / max offset, or whether
  pagination is offset/limit or cursor.

## What is already known here (dossier's gap list does not apply)

- **Tesco IE's gateway works from CI** — already proven by this repo, not by
  assumption: `search.api.tesco.com/search` + GraphQL hydration produce
  observations every collection run (9/9 mapped cells observed in run
  `35540088488`). The open question is *browseCategory + pagination*, not
  whether IE shares the gateway/apikey namespace.
- GTIN exposure at Tesco IE is confirmed independently of the dossier: the
  identity tier already records 10 GTIN hits at Tesco (research notes
  2026-09-20).
- Rate limits: the dossier finds no public number; this repo's existing
  throttle is the operating evidence (Tesco path is request-capped and spaced
  in `collect.yml` / `rediscover.yml`).

## What is wrong

- "No Irish equivalent tool found to exist publicly" contradicts the dossier's
  own §4.2, which names **SavvySpender.ie** (per-product `/compare-prices/`
  pages, 4-retailer table with per-litre unit pricing) and
  **MasterMarketApp.com** (weekly full-basket price index). Both are live Irish
  comparison services. Correct reading: no *soft-drinks-only* comparison site
  found; the basket-wide space is occupied.
- The recommended capture method ("DevTools against tesco.ie") conflicts with
  this repo's hard rule: **never probe a retailer from the home IP** (Akamai
  flagged Tesco; Dunnes 403'd it). Do the capture from CI egress instead — see
  next action. A human browsing tesco.ie in a normal browser is not a probe,
  but the automating path must not run here.

## Next action (ff-20)

1. CI-side probe, zero-risk to the home IP: one workflow run that issues a
   `browseCategory`-shaped query against the Fizzy Drinks aisle path, dumps the
   normalized response + pagination block as an artifact (same pattern as
   `canary.yml --dump-fixtures`). Do not build the adapter until the response
   shape is captured — the dossier's own caveat, and repo standard.
2. If the shape confirms: implement `TescoDiscoveryAdapter.walk_drinks`
   mirroring the Lidl/Aldi seams, feed the aisle pool through the normal
   classification + exact-pack bar (never straight to observations), then run
   `rediscover.yml` with `mode=first_discovery`, `retailer=tesco`, aimed at the
   23 Dunnes+SuperValu packs.
3. Acceptance: mapping coverage at Tesco rises above 9/100 with class-A/B
   evidence, and at least some of the 23 two-retailer packs become three-way
   comparable in `collection-batch.json`.

Fallback if browseCategory is not usable on IE: `first_discovery` term
expansion (search formulations) at a raised cap is already supported and costs
nothing to try first.
