# Aldi/Lidl category-walk probe — GO/NO-GO (2026-09-20)

One manual browser session (Feilim-approved, egress aldi.ie/lidl.ie only).
Capture technique: `agent-browser` HAR recording with response bodies, plus
one direct JSON fetch for the Lidl search API. Fixtures saved under
`research/` — nothing automated after this session.

## Verdict: **GO** for both retailers — real category JSON exists for both.

The deferral premise ("brand search returns junk") no longer holds: both
sites expose structured, paginated listing APIs that a *category walk* can
enumerate. Neither needs authentication, captcha, or a headful session
beyond this probe.

## Aldi — cleanest find

`aldi.ie` is no longer SAP-OCC behind `/occ/`/`/rest/` (the SmartCart spike
era). The Nuxt storefront now calls the **ASL commerce API** with plain JSON:

```
GET https://asl.api.aldi.ie/commerce/v3/product-search
    ?currency=EUR&serviceType=walk-in
    &categoryKey=1588161416978079&limit=30&offset=0
    &sort=relevance&servicePoint=D105
GET https://asl.api.aldi.ie/commerce/v2/product-category-tree
    ?serviceType=walk-in&servicePoint=D105
```

- Captured live in `research/aldi-occ-capture-2026-09-20.json`
  (+ raw `research/aldi-occ-capture-2026-09-20.har`).
- Pagination meta: `meta.pagination {offset, limit, totalCount: 54}`.
- Item shape: `sku` (same zero-padded numeric SKU our Aldi candidate IDs use,
  e.g. `000000000000336121`), `name`, `brandName`, `price.amount` (cents),
  `price.amountRelevantDisplay` ("€1.45"), `price.bottleDeposit`, an
  `alcohol` flag (glossary Drink excludes alcohol — free filter),
  `notForSale`, `categories`.
- Category tree carries stable keys: `Drinks/Soft Drinks & Juices`,
  `Drinks/Water` (plus Tea/Coffee/Hot Chocolate — out of scope for Drink).
- Note: `servicePoint=D105` (a store code) appeared in the query — a client
  must check whether prices vary per service point and pin one (or the
  national default) deliberately.

## Lidl — two JSON surfaces, both usable

1. **Range-page SSR** (`lidl.ie/c/{slug}/s{id}`): `data-grid-data`
   attributes carry full product objects — `title`, `erpNumber`,
   `price.price` (decimal EUR), `gs1Attributes` (GTIN evidence),
   `multipack`. Verified on `/c/food-drink/s10068374` (12 tiles saved in
   `research/lidl-range-grid-data-2026-09-20.json`; raw
   `research/lidl-range-capture-2026-09-20.har`). The landing page is
   featured tiles, not the full range grid.
2. **Search JSON** (`research/lidl-search-capture-2026-09-20.json`, raw
   `research/lidl-search-capture-2026-09-20.har`):
   ```
   GET https://www.lidl.ie/q/api/search
       ?assortment=IE&locale=en_IE&version=v2.0.0&q=coca+cola
   ```
   Works from plain `urllib` with browser-ish headers (no cookies needed;
   a bare request gets HTTP 406). `items[].gridbox.data` carries the same
   product object as the SSR attributes. Search supports `offset`/
   `fetchsize` (max 1000) and facets — a category/breadth probe beats the
   per-brand searches that produced keyword noise.

The ERP numbers match our existing Lidl candidate IDs (`lidl:11258557`
style), so identity keys would line up with the current store.

## Recommendation

**GO**: one implementation ticket on the ff map per retailer — an Aldi
category walk over `product-search` (categoryKey stepping through
`Soft Drinks & Juices` + `Water`) and a Lidl range/search walk over
`q/api/search` (or `data-grid-data` scraping as fallback). Both need the
standard bar treatment: exact-pack validation on the name/price evidence,
junk gate, and the discovery pipeline — never direct Price Observations.
Implementation must NOT reuse this session's browser; it should use the
package's HTTP seam with fixtures from this probe as the test doubles
(precedent: `beverage_feed/lidl.py::_lidl_record`, `tests/test_lidl.py`).

## Probe hygiene

- Egress was aldi.ie + lidl.ie (and their first-party CDNs/analytics) only.
- No automated client was built; nothing is scheduled.
- Fixtures are local-only research artifacts.
