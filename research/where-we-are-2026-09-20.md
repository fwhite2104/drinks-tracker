# Where we are — plain-English map (2026-09-20)

One page, no jargon. Read this if the ticket files have become noise.

## The goal, in one sentence

An app that answers "which Irish shop is this drink cheapest in, right now?"
— built on a feed of ~100 specific drink packs, each priced at each retailer,
every price verified to be the exact product (never a different size, pack
count, or version).

## The three moving parts

1. **The pipeline** (`beverage_feed/`) — *discovery* finds candidate products
   at each shop and maps them to catalog entries; *collection* fetches prices
   daily on GitHub Actions; a human (you) approves/rejects ambiguous mappings
   via dashboard sprints.
2. **The mobile app** (`mobile/`) — code finished; blocked on named human
   steps: domain + Cloudflare tunnel, EAS builds, on-device pass, store
   accounts (`scripts/setup-accounts.sh`).
3. **You** — the only source of verdicts on ambiguous products and store
   accounts. Everything else runs unattended.

## What's alive right now

| Thing | State |
|---|---|
| Coverage grind (ff-07) | **84/500 cells approved** (dunnes 41%, supervalu 35%, tesco 8%). Waiting on ~12 ambiguous-candidate verdicts + ~35 dunnes zero-candidate cells + Tesco CI discovery runs |
| ff-15 junk rejection | **Mechanism built 2026-09-20** (`discovery block` CLI). Dashboard "block everywhere" button still to do |
| ff-16 variant expansion | Decided, not built — needs your catalog ruling (see "your open decisions") |
| Wrong-product hardening | **Done 2026-09-20**: one real incident found and fixed historically (see below); collection now rejects listing-name conflicts automatically |

## What's parked and why

- **Lidl/Aldi** — deferred because brand search returns junk. SmartCart spike
  (`research/smartcart-scraper-spike-2026-09-20.md`) found a possible way back
  (category walk / Aldi OCC endpoint). Needs one canary probe to decide.
- **Catalog size question** — scorecard says evidence supports ~30 truly
  comparable packs, not 100. Decision interacts with ff-16.
- **Mobile human chain** — nothing for agents to do; it's your accounts.

## Your open decisions (whenever you get to them)

1. Verdicts on ~12 genuinely ambiguous candidates (dashboard review sprint).
2. Catalog ruling: shrink to ~30 comparable core, stay at 100, or expand
   variants (ff-16)?
3. Mobile chain steps (domain, store accounts).

## Recent wrong-product incident (the reason for this session's work)

For five collection runs the Dunnes single-can Coca-Cola cell recorded a
**€12.00 12-pack price** as the single can's price. Cause: the approved
mapping pointed at the wrong listing; no automated layer checked the "12 x"
in the name against the pack. Fixed 09-03 by hand; on 09-20 a composition
guard was added so collection now fails such observations automatically.
Details: `research/wrong-product-audit-2026-09-20.md`.

## Where everything lives

- Decisions/history: `.scratch/full-feed-coverage/` (map + 18 tickets).
- Domain glossary: `CONTEXT.md`; standards: `CONTRIBUTING.md`.
- Curated inputs: `data/` (catalog, mappings, rejections, feed DB).
- Read-only research reports: `research/`.
