# Drinks Tracker — Domain Glossary

## Drink
Soft drink or grocery beverage: carbonated drinks, juices, bottled water, energy drinks, sports drinks, and similar packaged beverages sold in retail stores. Excludes alcoholic beverages.

## Retailer (Tier 1)
National grocery chain operating in Ireland with a public online presence and structured or semi-structured product/pricing data. Examples: Tesco IE, Dunnes Stores, SuperValu, Lidl, Aldi.

## Retailer (Tier 2)
Independent shop, cafe, convenience store, or small grocer in Ireland. Typically lacks national online infrastructure; pricing data, if available, comes through delivery platforms, local aggregators, or manual collection.

## Area Coverage
Two-tier geographic model:
- **Tier 1**: Country-wide or region-level pricing (where national chains set prices at that granularity).
- **Tier 2**: Radius-based coverage for independent retailers near the user's location.

## Price Feed
The continuously or periodically updated collection of drink prices from retailers, consumed by the app to display comparisons.

## Catalog
**Benchmark Catalog**:
The initial standardized set of approximately 100 beverage products used to validate cross-retailer price collection and comparison. Each entry represents a specific sellable pack variant, not a generic beverage identity. It is independent of any retailer's raw SKU list and can expand toward broad beverage coverage over time.

**Catalog Mapping**:
An approved relationship between one Benchmark Catalog pack and one retailer listing. A mapping is valid only when the listing represents the same brand, variant, pack count, and unit size as the catalog pack.

**Dormant Catalog Mapping**:
A Catalog Mapping for which no Price Observation has been recorded for six months. It is hidden from normal history views and may be purged, along with its detailed observations, after twelve months without a new observation; the Benchmark Catalog identity remains eligible for remapping.

**Brand Alias**:
A curated mapping from a retailer's product-name identity to the catalog's canonical brand and variant (e.g. "Diet Coke" → brand Coca-Cola, variant Diet). Brand Aliases are applied when translating a listing into catalog identity, before the exact-pack agreement bar is applied; they never weaken the bar itself. Auto-suggested aliases require curation before use.

**Catalog Candidate**:
A retailer listing that may represent a new Benchmark Catalog pack but has not yet been approved for cross-retailer comparison. It has one canonical retailer/source identity and may be associated with multiple Catalog Pack cells that surfaced it.

## Catalog Mapping Discovery
**Discovery Run**: A bounded evaluation of retailer searches for active, not-yet-mapped Catalog Pack cells. It records what was searched and what evidence was found; it does not itself create Price Observations.

**Review Decision**: An operator decision about candidate evidence. It may approve one exact-pack mapping, reject a listing, or explicitly exclude a retailer–Catalog Pack cell. A review decision is distinct from a missing observation or an availability claim.

**Mapping Challenge**: New candidate evidence that appears to match a cell with an existing approved Catalog Mapping. It requires explicit operator resolution and does not replace the existing mapping automatically.

## Price Comparison
**Exact-Pack Comparison**:
The default comparison mode, comparing the same specific pack variant across retailers by its total retail price.

**Component Unit Price**:
A derived price for one saleable item inside a multipack, such as the price per can. It supplements the exact-pack price and does not replace it.

**Price Observation**:
A successful, timestamped observation of a specific pack's price at one retailer and optional source scope, such as a store. Observations are retained over time; a failed collection run does not create or replace an observation. The absence of an observation is not an inventory claim.

**Collection Result**:
The outcome for one retailer and one Benchmark Catalog pack during a collection run. A result may be observed, not found, source error, or unmapped; only observed results are current-feed prices.

**Current Feed**:
The latest successfully observed retailer-by-pack prices. A pack absent from the latest successful result is omitted from the Current Feed, not treated as permanently retired.

**Last Seen**:
The timestamp and details of the latest successful Price Observation for a retailer-by-pack combination, retained for historical reference after it leaves the Current Feed.

**Displayed Price**:
The non-member beverage price shown by a retailer at the time of collection, whether it is the retailer's usual price or a temporary discount. It is not assumed to be a permanent regular price and excludes a separately identified DRS Deposit.

**DRS Deposit**:
The refundable container deposit associated with an eligible beverage pack, recorded separately from its Displayed Price.

**Clubcard Price**:
A retailer price available only to a qualifying loyalty-program member, recorded separately from the Displayed Price.
