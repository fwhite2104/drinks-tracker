# Launch dossier — legal posture, retailer surfaces, SEO, identity (2026-09-20)

Second half of the Perplexity dossier Feilim ran on 2026-09-20. This is the
"do we go public, and how" material. Sourcing is the dossier's; the reading,
corrections and repo implications are ours.

## 1. Legal exposure (matters before the public URL is live)

Retailer contract positions, as found:

| Retailer | Position | Risk |
|---|---|---|
| Dunnes | ToS explicitly prohibit "using automated systems or software to extract data from this Website for commercial purposes ('screen scraping')" without licence | Clearest prohibition of the three |
| Tesco IE | No scraping-specific term; IP clause bars copying/using content "for commercial purposes" without permission, plus a ban on unauthorised system access | Ambiguous but restrictive |
| SuperValu (Musgrave) | Content "for personal, non-commercial use"; no reproduction/exploitation "in any way" without permission | Broad, same effect as Tesco |

None of the three block these paths in robots.txt (Tesco.ie disallows only a
narrow promotions-query pattern) — weak positive signal only; it does not
override an explicit ToS clause.

Case law the dossier cites — **Ryanair v PR Aviation (CJEU C-30/14, 2015)** is
the operative precedent: where a database has neither copyright nor sui generis
protection, the Database Directive's user protections do not apply, so the
operator can enforce an anti-scraping ToS by ordinary contract law. Translation
for this project: the Dunnes clause is independently enforceable as a contract
term regardless of whether their catalogue is a protected database. `Innoweb`
(C-202/12) adds that systematic re-presentation of another site's data can be
re-utilisation in its own right if the database qualifies.

No Irish court decision and no CCPC action on grocery-price scraping was found.
That is a real gap, not a green light.

Practical mitigations the dossier recommends, all consistent with what this repo
already does or can cheaply do:
- keep collection low-volume and spaced (already the case; CI egress, request
  caps, canary gate);
- attribute every price with source + observation timestamp (already in the
  feed contract);
- no resale, no bulk redistribution of the raw dataset — the public surface
  shows derived comparison, not a data dump;
- takedown readiness: all three retailers reserve the right to suspend access
  without notice; expect a cease-and-desist letter first, not litigation, and
  have a "pause that retailer within a day" path.
- avoid extracting a "substantial part" repeatedly (Database Directive art. 8).

**Open decision for Feilim (blocking public launch).** The MVP web app can be
published as planned, with Dunnes as the named exposure — or the launch can
ship with Dunnes excluded from the public surface while collection continues
privately. Recommendation: publish as planned (free, ad-light, attributed,
takedown-ready), because the value of the product is the comparison and Dunnes
is one of three; but this is a risk-acceptance call only Feilim can make, and
it should be recorded in the ff map before the tunnel goes up.

## 2. Retailer data-surface map (expansion order, post-MVP)

Confirmed workable or strongly indicated:
- **Lidl IE / Aldi IE** — multiple 2025-2026 scrapers, plus this repo's own
  category-walk GO verdict (2026-09-20) with captured JSON. Add next, in that
  order, as already ticketed on the ff map.
- **Donnybrook Fair** — URL structure indicates Shopify; `/products.json` and
  `/collections/<handle>/products.json` are the first things to try. Not
  verified by the dossier; a cheap probe.
- Aggregators (Buymie/Dunnes delivery, Deliveroo, Uber Eats, Just Eat) —
  per-branch pages only, no national feed, each with its own bot defences.
  Not a data source for v1.

No known automated path: Centra/Spar product pricing (only store-locator
metadata on a public Musgrave dev subdomain), Londis per-store ordering,
Costcutter's full range (a small WooCommerce subset is paged HTML at best).
Treat the whole convenience tier as reverse-engineering projects with no prior
art.

## 3. SEO and launch content

Query shapes that matter: `brand + size + price + ireland`, `cheapest <brand>
ireland`, and retailer-vs-retailer comparisons. Page plan: one page per pack
("Coca-Cola 2L price Ireland") listing all three retailers side by side, plus a
retailer hub page ("Tesco soft drinks prices") interlinking them.

Competitors to expect in the SERP: SavvySpender.ie, MasterMarketApp.com,
Klarna.ie (unattributed aggregator), the retailers' own pages, and import/candy
shops for novelty flavours. The opening is the soft-drinks-only, pack-level
long tail — nobody dedicated does it.

Schema.org: one `Product` with **one `Offer` per retailer** (not a single
`AggregateOffer`), each with `price`, `priceCurrency: "EUR"`, `availability`,
`url`. The JSON-LD price must match the visible price exactly or Google drops
the rich result — which means the page render must be driven by the same
`/consumer/feed` payload, never a second source.

Linkability: StarDeals.ie and the long-running Boards.ie grocery-price threads
share per-litre framing and timestamped deals. The repeatable hook is a weekly
"cheapest soft drinks basket" digest — cadence beats a one-off launch post.

## 4. GTIN / pack identity tiers (revises current practice)

- **Primary**: the retailer's own GTIN when the listing exposes it (confirmed at
  Tesco IE; unconfirmed at Dunnes/SuperValu — verify before relying on it).
  Same source as the price, no external dependency, no rate limit.
- **Fallback**: normalized brand + variant + pack count + unit size + package
  type (already implemented).
- **Spot-check only**: GS1 "Verified by GS1" — 30 free queries/day, and coverage
  is opt-in by the manufacturer, so a valid barcode can return nothing. Never
  bulk-validate through it.
- **Enrichment only**: Open Food Facts — good for nutrition/ingredient display,
  explicitly not a matching key (no accuracy guarantee, no confirmed IE
  coverage). If bulk lookup is ever needed, use their downloadable dump, not
  the live API.

Nothing here changes the exact-pack bar; it sharpens which evidence sits at the
top of the identity tier.
