# SmartCart scraper spike — 2026-09-20

Read-only review of [Richard/SmartCart](https://git.richardnixon.dev/Richard/SmartCart)
(self-hosted Forgejo, **no license file** → techniques only, never copy code).
Clone was made to `/tmp/opencode/smartcart` and deleted after this report was
written; the citations below were taken during the session and the source is
recoverable by re-cloning if ever needed.

SmartCart is a price-comparison engine for the same five Irish retailers we
track, but it takes a **Playwright category-walk** architecture rather than our
first-party-JSON search architecture. This spike extracts what is transferable
to `beverage_feed/`.

---

## 1. The headline finding: Lidl/Aldi category walk

`goal.md` defers Lidl/Aldi because *brand-term search yields only keyword
noise* — that is a property of the **search** surface, not the retailer.
SmartCart never searches; it **walks category pages**, where every product is
in-scope by construction. This directly challenges the deferral premise.

### Lidl Ireland (SmartCart `src/scrapers/lidl.py`)

- **Two page flavours** (docstring, lines 1–16):
  - Campaign/offer pages `https://www.lidl.ie/c/{slug}/a{id}` — **server-side
    rendered**, product tiles in the initial HTML; plain `httpx` works, no
    browser needed.
  - Static range pages `.../c/{slug}/s{id}` — fully client-rendered (Nuxt
    hydration); needs a JS-rendering browser.
- **Category discovery**: GET `https://www.lidl.ie/grocery-range` and scrape
  all `a[href*='/c/']` links (lines 94–120). httpx works for discovery.
- **Extraction** (the key trick, lines 225–250): Lidl embeds a full JSON blob
  per product in the **`data-grid-data` attribute** of
  `div.AProductGridbox__GridTilePlaceholder` elements. The visible HTML is
  skeleton placeholders; all data is in the attribute. Parsed fields include:
  `fullTitle`/`title` (name), `productId`/`itemId`/`erpNumber` (SKU),
  `canonicalUrl`, `price.price`, **`lidlPlus[0].price`** (member-price with
  `deletedPrice`/`oldPrice` = shelf price), `basePrice.price` (unit price),
  `packaging.text`, `brand.name`, **`ians[0]` (EAN)**,
  `stockAvailability.availabilityIndicator` (>2 = out of stock), `ribbons`
  (promo labels).
- **Relevance to us**: this is a second, independent Lidl source. Our
  `beverage_feed/lidl.py` uses the hidden search API
  (`version`/`locale`/`assortment` params, keyword noise). A category walk
  over `/a{id}` campaign pages + `/s{id}` range pages would enumerate the
  actual assortment once per run — a catalog-coverage source, not a
  per-cell search. The EAN field also enables exact-pack identity checks.

### Aldi Ireland (SmartCart `src/scrapers/aldi.py`)

- Static category paths confirmed (lines 33–47), notably
  **`/products/drinks/`** — the walk is a fixed list of ~13 top-level
  categories, paginated via `a[rel='next']`.
- httpx + BeautifulSoup works for standard category pages (lines 90–137);
  `https://www.aldi.ie/specials` needs JS rendering.
- **OCC API interception** (lines 261–282): while a page loads, intercept
  responses whose URL contains `/occ/` or `/rest/` — Aldi runs **SAP
  Commerce** and the storefront calls JSON OCC endpoints with a `products`
  array. Parsed fields: `code`, `name`, `price.value`, `wasPrice.value`
  (was-price → promo), `basePrice`, `images[].url`, `brand`.
- **Relevance to us**: our `beverage_feed/aldi.py` uses
  `asl.api.aldi.ie` Glue endpoints (search, keyword noise). The OCC
  interception suggests there is a category/listing JSON endpoint behind the
  storefront that returns products by category, not by search term — worth a
  canary probe (`python -m beverage_feed canary --retailer aldi`) before
  building anything.

### Tesco Ireland (SmartCart `src/scrapers/tesco.py`)

- Confirms our TescoClient's diagnosis: Akamai WAF + **obfuscated CSS module
  class names that change every build** (lines 38–44). Their answer is
  browser + structural JS extraction over `ul#list-content`, product links
  matching `/products/{id}`, price text matched by `€` prefix patterns
  (lines 46–140).
- Category slugs list (lines 29–41) includes **`drinks`** —
  `https://www.tesco.ie/groceries/en-IE/shop/drinks/all`.
- **Relevance to us**: weaker. Our GraphQL + curl-cffi path already works on
  CI and gives structured JSON (no DOM parsing). The category-walk idea is the
  transferable part: our Tesco discovery gap (8/100 cells) is a *search-term*
  gap; walking the `drinks` category (via the same GraphQL gateway, category
  filter instead of query) could enumerate candidates we never think to
  search for.

### Dunnes / SuperValu (skimmed)

- Dunnes: grocery site is `www.dunnesstoresgrocery.com` (not dunnesstores.com);
  categories `/categories/{slug}-id-{id}`, top-level only (29 not 1603).
  Playwright-only in SmartCart; we already have a working JSON client —
  nothing to take except the confirmation of the domain split.
- SuperValu: `shop.supervalu.ie`, category discovery from
  `/shopping/allaisles` (`a[href*='/categories/']` with `-id-`), requires
  login + store selection before browsing. We already have a working client;
  the allaisles page is a nice enumeration source if we ever need category
  completeness.

---

## 2. Cross-store matcher (SmartCart `src/matcher/`)

Three-level matching: (1) exact EAN, (2) `rapidfuzz.fuzz.token_sort_ratio`
≥ 85 on normalized names, (3) unit-info cross-check — reject the fuzzy match
if parsed unit/size differ (`matcher.py:76-106`). Not new to us (our
discovery pipeline + review CLI already handles exact-pack mapping with human
verdicts), but the **EAN-first** ordering is a validation we could add as an
evidence class: Lidl's `data-grid-data` exposes `ians[0]`; if other sources
expose EAN, exact-pack identity becomes provable, not just name-judged.

## 3. What I recommend (go/no-go)

**GO — narrow spike, one retailer:** probe whether an Aldi category-listing
JSON endpoint exists behind `/products/drinks/` (OCC interception during one
manual Playwright session, capture the URL). If yes, Aldi's deferral premise
(keyword noise) is void and ff-07 gains a fourth retailer cheaply.

**CONDITIONAL GO — Lidl:** the `/a{id}` campaign pages are plain-HTML
scrapable, but they are *offers*, not the full range; `/s{id}` range pages
need JS rendering we don't currently ship on CI. Recommend a small canary
probe of one `/s{id}` drinks page before committing.

**NO-GO — adopting SmartCart code wholesale.** Different architecture
(browser-first vs our API-first), no license, and it would drag Playwright
into our CI for all retailers. Port *techniques* (endpoints, selectors,
field maps) into our existing client/fixture pattern only.

**Side note (mobile chain):** JustPaddy/grocery-price-data publishes
periodically-refreshed SQLite via GitHub Releases for an app to sync — a
pattern worth remembering if the Cloudflare-tunnel feed (ff ticket 10) ever
proves fragile. Not actionable now.

## 4. Source index (for re-cloning)

| File | Lines of interest |
|---|---|
| `src/scrapers/lidl.py` | 1–16 page flavours; 94–120 discovery; 225–250 `data-grid-data`; 300–360 Lidl Plus price |
| `src/scrapers/aldi.py` | 33–47 category paths; 261–282 OCC interception; 284–378 OCC parse |
| `src/scrapers/tesco.py` | 29–41 category slugs; 46–140 JS extraction rationale |
| `src/scrapers/dunnes.py` | 28–37 domain + category format |
| `src/scrapers/supervalu.py` | 80–99 allaisles discovery |
| `src/matcher/matcher.py` | 76–106 match strategy |
