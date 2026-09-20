# Wrong-product audit — observed listings vs catalog packs (2026-09-20)

Read-only join of `data/feed.sqlite` `price_observations` × `catalog_packs`
(314 observed rows) plus a phrasing-consistency scan of `data/mappings.json`
approved rows against `data/catalog.json`. Zero retailer egress.

## Findings

### 1. Confirmed incident: single-can cell observed the 12-pack price (dunnes)

`coca-original-330-single` (catalog: single 330ml can) was observed **five
times** (2026-08-24 → 2026-09-03) with `source_product_name` = **"Coca-Cola
Original Taste 12 x 330ml"** and `displayed_price` = **€12.00** recorded as the
single can's price (`component_unit_price` also €12.00). Reference pinned:
`100298438` — the 12-pack listing.

- Cause: the approved mapping pointed at the wrong listing (mapping
  correctness, not a fetch bug). Collection's `_validate_listing` passes any
  same-brand name ("coca cola" ⊆ "coca cola original taste 12 x 330ml") and
  no layer parsed the "12 x" token against the pack's `pack_count=1`.
- Fixed: 2026-09-03 agent-range-sprint re-mapped the cell to `100298009`
  ("single 330ml can, multipacks excluded"); latest observation is correct
  (€1.65, `100298009`).
- Residue: the five bad rows remain in `price_observations` history. The
  current feed renders only the latest observation per cell, so **the live
  feed is correct today**; history is polluted but unreferenced.

### 2. Latent mapping scan: 0 live conflicts

- **Pack-count phrasing**: 13 approved mappings phrase the multipack
  differently than the catalog ("24 x 330ml" vs "330ml Cans x24") — all 13
  agree on multipack-ness on both sides; benign.
- **Size (ml/l)**: 0 observations where the observed listing's size token
  conflicts with the catalog `unit_size_ml`.

### 3. Why the guard is still needed

The incident was caught by a human sprint, not by the pipeline. The same
failure mode (mapping pins a sibling-size listing) is invisible to every
automated layer today: `exact_match` requires structured attributes the
collection path doesn't recheck, `_validate_listing` is brand-only by design,
and the canary runs only at release time. Until listing pack-count/size tokens
are checked at collection time, a re-pinned or drifted mapping can record a
wrong price for days again.

## Recommended action (implemented after this audit)

Composition guard at the shared seam (`_validate_listing` +
pack-count-aware finders): parse "N x size" / "xN" / "N pack" tokens from the
observed listing name and fail on conflict with the catalog pack. Root-cause:
one function, all five retailers.
