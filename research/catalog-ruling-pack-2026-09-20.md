# Catalog ruling pack (R2)

Generated 2026-09-20T20:27:29Z by `beverage_feed/catalog_ruling.py` — read-only join of `data/catalog.json` × the discovery database. Zero retailer requests. **Feilim rules; no `data/catalog.json` edit happened** — the JSON decision files are identity of record until a human edits the catalog.

## Headline

- Packs scored: **100**
- Comparable (≥2 retailers with evidence): **71**
- Proven (≥2 approved Catalog Mappings): **31**

## Draft catalogs (remaining cells = 5 retailers − approved mappings − do_not_map rejections)

| option | what | packs | cells left |
|---|---|---|---|
| a | comparable core (proven first, then ranked comparable) | 31 | 87 |
| b | stay-at-100 (unchanged) | 100 | 355 |
| c | core + 10 new variant packs (added cell cost: 10 × 5 = 50) | 41 | 137 |

vs the current 100: (a) drops 69 cells-laden single-retailer/no-signal packs and cuts the cell load by 268; (c) keeps that core and re-adds 10 evidence-backed variant packs for 50 extra cells.

### Option (c) new Catalog Packs (from ff-16 evidence)

| new pack | ff-16 family | family listings | retailers |
|---|---|---|---|
| 7UP Zero Sugar 330ml Can | 7up: zero | 8 | dunnes |
| 7UP Zero Sugar Bottle 500ml | 7up: zero | 8 | dunnes |
| 7UP Zero Sugar Bottle 1.25 Litres | 7up: zero | 8 | dunnes |
| 7UP Zero Sugar Bottle 2 Litres | 7up: zero | 8 | dunnes |
| Dr Pepper Zero 500ml | dr pepper: zero | 2 | dunnes |
| Dr Pepper Zero 2 Litres | dr pepper: zero | 2 | dunnes |
| Red Bull Sugarfree 355ml | red bull: sugar free | 3 | dunnes |
| Lucozade Zero Sugar 380ml | lucozade: zero | 5 | dunnes |
| Volvic Touch of Fruit Sugar Free 1.5 Litres | volvic: sugar free | 2 | dunnes |
| Pepsi Cream Soda Zero Sugar 330ml Can | pepsi: zero | 1 | dunnes |

## Per-retailer evidence strength

| retailer | candidates | approved mappings | listing rejections | GTIN hits |
|---|---|---|---|---|
| dunnes | 692 | 41 | 106 | 0 |
| supervalu | 204 | 36 | 347 | 0 |
| tesco | 10 | 9 | 0 | 10 |
| lidl | 16 | 0 | 66 | 0 |
| aldi | 83 | 0 | 316 | 0 |

## ff-16 variant evidence (named by listings, no catalog cell)

A listing counts for a family when its name mentions a catalog brand, states a variant keyword, and states none of the variant/alias phrasings that brand already has in the catalog. Rejected listings are excluded. (Known strict (brand,variant)-pair gap: Jack Daniel's & Coca-Cola Zero Sugar — 3 SuperValu listings, already rejected, so it does not appear below.) Junk siblings (e.g. Berocca) are review-time rejects under ff-15, not catalog material.

| family | listings | retailers | example listing names |
|---|---|---|---|
| 7up: zero | 8 | dunnes | `7Up Zero Sugar Can 12 x 330ml`<br>`7Up Zero Sugar Can 18 x 330ml`<br>`7Up Zero Sugar Can 330ml`<br>`7Up Zero Sugar Can 4 x 330ml`<br>`7Up Zero Sugar Can 8 x 330ml` |
| lucozade: zero | 5 | dunnes | `Lucozade Energy Zero Sugar Drink Grafruitti 500ml`<br>`Lucozade Energy Zero Sugar Drink Original 380ml`<br>`Lucozade Energy Zero Sugar Drink Original 4x380ml PMP €5`<br>`Lucozade Energy Zero Sugar Drink Original 500ml`<br>`Lucozade Energy Zero Sugar Drink Original 6x380ml` |
| 7up: lemonade/pink/zero | 3 | dunnes | `7Up Zero Sugar Pink Lemonade 2L`<br>`7Up Zero Sugar Pink Lemonade Bottle 500ml`<br>`7Up Zero Sugar Pink Lemonade Can 330ml` |
| lucozade: lemonade/pink/zero | 3 | dunnes | `Lucozade Energy Zero Sugar Drink Pink Lemonade 330ml PMP €1.50`<br>`Lucozade Energy Zero Sugar Drink Pink Lemonade 500ml`<br>`Lucozade Energy Zero Sugar Drink Pink Lemonade 900ml` |
| red bull: sugar free | 3 | dunnes | `Red Bull Energy Drink, Sugar Free, 250ml`<br>`Red Bull Energy Drink, Sugar Free, 355ml`<br>`Red Bull Sugar Free 4 x 250ml` |
| tropicana: tropical | 3 | dunnes | `Tropicana Fresh & Light Tropical Juice Blend Drink 850ml`<br>`Tropicana Tropical Fruit Juice 1.5L`<br>`Tropicana Tropical Fruit Juice 850ml` |
| dr pepper: zero | 2 | dunnes | `Dr Pepper Zero 2L`<br>`Dr Pepper Zero 500ml` |
| red bull: zero | 2 | dunnes | `Red Bull Zero 250ml`<br>`Red Bull Zero 4 x 250ml` |
| red bull: vanilla | 2 | dunnes | `Red Bull Iced Vanilla Berry`<br>`Red Bull The Ice Edition Iced Vanilla Berry Energy Drink 4 x 250ml` |
| red bull: tropical | 2 | dunnes | `Red Bull Energy Drink Tropical Edition 250ml`<br>`Red Bull The Tropical Edition Tropical Fruits Energy Drink 4 x 250ml` |
| volvic: sugar free | 2 | dunnes | `Volvic Touch of Fruit Sugar Free Mango Passion 1.5L`<br>`Volvic Touch of Fruit Sugar Free Summer Fruits 1.5L` |
| pepsi: zero | 1 | dunnes | `Pepsi Cream Soda Flavour Zero Sugar Can 330ml` |
| coca cola: free/zero | 1 | dunnes | `Coca-Cola Zero Caffeine Free 500ml` |
| fanta: cherry | 1 | dunnes | `Fanta Crimson Cherry Apple Cherry Taste 1.75L` |
| boost: free | 1 | dunnes | `Berocca Boost 1 Tube Extra Free, Vitamin C & B12, Effervescent Tablets 45` |
| boost: cherry | 1 | dunnes | `Berocca Boost Effervescent Vitamin Tablets with guarana, caffeine, vitamin B12, vitamin C & magnesium. Cherry flavour.  15 tablets.` |
| ballygowan: tropical | 1 | dunnes | `Ballygowan B Active Tropical Rush Caffeine Water 500ml` |

### Option (a) core packs

- `ballygowan-sparkling-500`
- `ballygowan-sparkling-6`
- `ballygowan-still-1500`
- `ballygowan-still-6`
- `capri-orange-10`
- `club-orange-330`
- `coca-diet-2000`
- `coca-diet-330-24`
- `coca-diet-330-single`
- `coca-diet-500`
- `coca-original-330-24`
- `coca-original-330-single`
- `coca-original-500`
- `coca-zero-2000`
- `coca-zero-330-24`
- `coca-zero-330-single`
- `coca-zero-500`
- `evian-6`
- `innocent-apple-900`
- `lucozade-orange-500`
- `lucozade-sport-orange-500`
- `lucozade-sport-orange-500-2`
- `miwadi-orange-1l`
- `monster-mango-500`
- `monster-original-500`
- `monster-pipeline-500`
- `monster-ultra-white-500`
- `monster-zero-500`
- `schweppes-slimline-1l`
- `tropicana-apple-900`
- `volvic-1500`
