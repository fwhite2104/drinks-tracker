# Goal

## What this app is

An anonymous Android + iOS app for shoppers in Ireland: pick a soft drink (a
Benchmark Catalog pack), see which Tier-1 retailer is cheapest right now via
Exact-Pack Comparison. The app consumes the Price Feed and nothing else — it
never collects, discovers, or mutates data.

## The user experience we want

1. **Find a drink fast.** Cold open lands on a search-first catalog: one
   search bar, brand-grouped card grid, cheapest-known price on every card.
   A specific pack is a few characters of typing away.
2. **Answer one question decisively:** "where is this cheapest today?" The
   cheapest retailer is the hero of the comparison screen — big card, big
   price. Everyone else is a smaller row. No scanning, no ambiguity.
3. **Trust every number shown.** Every price carries its observation date;
   stale prices say "may be out of date" after 7 days. Member (Clubcard)
   prices sit beside, never instead of, the public price. The DRS deposit is
   always its own line, never folded into the drink's price.
4. **Never lie about absence.** A missing price is exactly one of five honest
   states (`observed`, `last_seen`, `awaiting_price`,
   `temporarily_unavailable`, `not_available`) — never a stock claim, never a
   retirement claim, never a synthetic or demo price. "Last seen <date>" is a
   fact, not a price we reuse.
5. **Work offline, honestly.** Cached prices render immediately, labelled
   "as of <date>", with a non-blocking banner when refresh fails. The app
   shows what it last knew and says so — no invented data in any state.

## The product experience we want

- **Trust is the product.** The feed's value is that shoppers believe it. A
  single misleading price, invented state, or folded-in deposit destroys
  more than that price. Server owns the state machine; the app only renders.
- **Small catalog, fully true beats broad catalog, partly guessed.** ~100
  packs, every one verified like-for-like across retailers. The app is
  truthful at any coverage level and grows useful as mappings grow.
- **Cheap to run, cheap to trust.** Collection runs where egress is safe
  (GitHub Actions), the public API is a tunnel from one VM, auth is none,
  and a manual live canary gates releases. Operational machinery stays
  invisible to the shopper.
- **Anonymous by default.** No accounts, no favorites, no sync, no tracking
  surface. Open the app, find the price, close it.

## Non-goals (v1)

Alcohol; Tier-2/independent retailers; accounts, favorites, or sync;
notifications; barcode scanning; history charts (Last Seen only); operator
flows in the app; synthetic prices anywhere.

## Definition of success

A shopper in Ireland opens the app, types "zero", sees Coca-Cola Zero Sugar
8×330ml cheapest at Tesco with per-can and deposit spelled out and the
Clubcard pill beside it, trusts the date it was observed, and buys from
Dunnes instead because the app told them it was cheaper. Offline, in a
lift, on first launch: the app is honest in all three.
