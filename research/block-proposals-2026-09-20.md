# Retailer block proposals — 2026-09-20T20:07:53Z

**Applied this run:** 82 blocks (421 cells cleared), 0 skipped (decided_by=agent-block-pass; Feilim spot-checks).

ff-15 R1b: mechanical pass over `data/rejections.json` (zero egress).
A block is proposed when one candidate was cell-rejected in ≥2 cells and
every reason is junk (not a beverage / wrong product) — never a variant
dispute. Variant-ambiguous groups stay in Feilim's batch.

- unambiguous junk proposals: **82**
- variant-ambiguous (Feilim batch): **88**
- cell-scoped rows covered by the proposals: **417**

## Junk proposals (auto-applicable via `agent-block-pass`)

| retailer | candidate | cells | reason |
|---|---|---|---|
| aldi | `aldi:000000000000227567` | 2 | agent-sprint: different product category and brand, keyword noise match |
| aldi | `aldi:000000000000227581` | 2 | agent-sprint: different product category and brand, keyword noise match |
| aldi | `aldi:000000000000227587` | 2 | agent-sprint: different product category and brand, keyword noise match |
| aldi | `aldi:000000000000227713` | 2 | agent-sprint: different product category and brand, keyword noise match |
| aldi | `aldi:000000000000278593` | 2 | agent-sprint: different product category and brand, keyword noise match |
| aldi | `aldi:000000000000278692` | 2 | agent-sprint: different product category and brand, keyword noise match |
| aldi | `aldi:000000000000278707` | 2 | agent-sprint: different product category and brand, keyword noise match |
| aldi | `aldi:000000000000278727` | 2 | agent-sprint: different product category and brand, keyword noise match |
| aldi | `aldi:000000000000284569` | 2 | agent-sprint: different product category and brand, keyword noise match |
| aldi | `aldi:000000000000335578` | 2 | agent-sprint: different product category and brand, keyword noise match |
| aldi | `aldi:000000000000335769` | 2 | agent-sprint: different product category and brand, keyword noise match |
| aldi | `aldi:000000000000335770` | 2 | agent-sprint: different product category and brand, keyword noise match |
| aldi | `aldi:000000000000335783` | 2 | agent-sprint: different product category and brand, keyword noise match |
| aldi | `aldi:000000000000335801` | 7 | agent-sprint: different product category and brand, keyword noise match |
| aldi | `aldi:000000000000335859` | 2 | agent-sprint: different product category and brand, keyword noise match |
| aldi | `aldi:000000000000335880` | 2 | agent-sprint: different product category and brand, keyword noise match |
| aldi | `aldi:000000000000336441` | 2 | agent-sprint: different product category and brand, keyword noise match |
| aldi | `aldi:000000000000384741` | 7 | agent-sprint: different product category and brand, keyword noise match |
| aldi | `aldi:000000000000387515` | 7 | agent-sprint: different product category and brand, keyword noise match |
| aldi | `aldi:000000000000387518` | 7 | agent-sprint: different product category and brand, keyword noise match |
| aldi | `aldi:000000000000423157` | 7 | agent-sprint: different product category and brand, keyword noise match |
| aldi | `aldi:000000000000448511` | 7 | agent-sprint: different product category and brand, keyword noise match |
| aldi | `aldi:000000000000503592` | 2 | agent-sprint: different product category and brand, keyword noise match |
| aldi | `aldi:000000000000517319` | 2 | agent-sprint: different product category and brand, keyword noise match |
| aldi | `aldi:000000000000517392` | 2 | agent-sprint: different product category and brand, keyword noise match |
| aldi | `aldi:000000000000533154` | 2 | agent-sprint: different product category and brand, keyword noise match |
| aldi | `aldi:000000000000541852` | 2 | agent-sprint: different product category and brand, keyword noise match |
| aldi | `aldi:000000000000543613` | 2 | agent-sprint: different product category and brand, keyword noise match |
| aldi | `aldi:000000000000554714` | 3 | agent-sprint: beer, different brand and product |
| aldi | `aldi:000000000000573900` | 5 | agent-sprint: beer, different brand and product |
| aldi | `aldi:000000000000596929` | 2 | agent-sprint: different product category and brand, keyword noise match |
| aldi | `aldi:000000000000623466` | 2 | agent-sprint: different product category and brand, keyword noise match |
| aldi | `aldi:000000000000648917` | 7 | agent-sprint: different product category and brand, keyword noise match |
| aldi | `aldi:000000000270155001` | 7 | agent-sprint: different product category and brand, keyword noise match |
| aldi | `aldi:000000000335641002` | 7 | agent-sprint: different product category and brand, keyword noise match |
| aldi | `aldi:000000000336499001` | 7 | agent-sprint: different product category and brand, keyword noise match |
| aldi | `aldi:000000000337286001` | 7 | agent-sprint: different product category and brand, keyword noise match |
| aldi | `aldi:000000000342258003` | 7 | agent-sprint: different product category and brand, keyword noise match |
| aldi | `aldi:000000000345885001` | 2 | agent-sprint: different product category and brand, keyword noise match |
| aldi | `aldi:000000000347810001` | 7 | agent-sprint: different product category and brand, keyword noise match |
| aldi | `aldi:000000000400448001` | 7 | agent-sprint: different product category and brand, keyword noise match |
| aldi | `aldi:000000000401646001` | 7 | agent-sprint: different product category and brand, keyword noise match |
| aldi | `aldi:000000000403836002` | 5 | agent-sprint: different product category and brand, keyword noise match |
| aldi | `aldi:000000000404143005` | 15 | agent-sprint: cola-flavoured sweet/lolly, not Coca-Cola drink, wrong brand |
| aldi | `aldi:000000000405391002` | 15 | agent-sprint: cola-flavoured sweet/lolly, not Coca-Cola drink, wrong brand |
| aldi | `aldi:000000000405572002` | 3 | agent-sprint: different product category and brand, keyword noise match |
| aldi | `aldi:000000000422715002` | 7 | agent-sprint: different product category and brand, keyword noise match |
| aldi | `aldi:000000000495627001` | 7 | agent-sprint: different product category and brand, keyword noise match |
| aldi | `aldi:000000000495627002` | 7 | agent-sprint: different product category and brand, keyword noise match |
| aldi | `aldi:000000000522552001` | 7 | agent-sprint: different product category and brand, keyword noise match |
| aldi | `aldi:000000000561480001` | 15 | agent-sprint: cola-flavoured sweet/lolly, not Coca-Cola drink, wrong brand |
| aldi | `aldi:000000000612998001` | 7 | agent-sprint: different product category and brand, keyword noise match |
| aldi | `aldi:000000000628369001` | 7 | agent-sprint: different product category and brand, keyword noise match |
| aldi | `aldi:000000000631338001` | 7 | agent-sprint: different product category and brand, keyword noise match |
| aldi | `aldi:000000000689711001` | 7 | agent-sprint: different product category and brand, keyword noise match |
| aldi | `aldi:000000000707909001` | 15 | agent-sprint: cola-flavoured sweet/lolly, not Coca-Cola drink, wrong brand |
| aldi | `aldi:000000000743063002` | 15 | agent-sprint: cola-flavoured sweet/lolly, not Coca-Cola drink, wrong brand |
| lidl | `lidl:10062229` | 11 | agent-sprint: Different brand/product, not Coca-Cola pack |
| lidl | `lidl:10062231` | 5 | agent-sprint: Different brand/product, not Coca-Cola pack |
| lidl | `lidl:11003269` | 6 | agent-sprint: Different brand/product, not Coca-Cola pack |
| lidl | `lidl:11005482` | 3 | agent-sprint: Different brand/product, not Coca-Cola pack |
| lidl | `lidl:11039450` | 7 | agent-sprint: Different brand/product, not Coca-Cola pack |
| lidl | `lidl:11039715` | 2 | agent-sprint: Different brand/product, not Coca-Cola pack |
| lidl | `lidl:11040259` | 6 | agent-sprint: Different brand/product, not Coca-Cola pack |
| lidl | `lidl:11102123` | 11 | agent-sprint: Different brand/product, not Coca-Cola pack |
| lidl | `lidl:11146073` | 2 | agent-sprint: Different brand/product, not Coca-Cola pack |
| lidl | `lidl:11209878` | 4 | agent-sprint: Different brand/product, not Coca-Cola pack |
| lidl | `lidl:11219542` | 4 | agent-sprint: Different brand/product, not Coca-Cola pack |
| lidl | `lidl:11254625` | 7 | agent-sprint: Different brand/product, not Coca-Cola pack |
| lidl | `lidl:11400972` | 7 | agent-sprint: Different brand/product, not Coca-Cola pack |
| lidl | `lidl:11699531` | 7 | agent-sprint: Different brand/product, not Coca-Cola pack |
| supervalu | `supervalu:1999020000` | 3 | agent-sprint: wrong brand (Pepsi Cream Soda) |
| supervalu | `supervalu:1999022000` | 3 | agent-sprint: wrong brand (Pepsi Cream Soda) |
| supervalu | `supervalu:1999038000` | 3 | agent-sprint: wrong brand (Pepsi Cream Soda) |
| supervalu | `supervalu:1999041000` | 3 | agent-sprint: wrong brand (Pepsi Cream Soda) |
| supervalu | `supervalu:1999056000` | 3 | agent-sprint: wrong brand (Pepsi Strawberries 'N' Cream) |
| supervalu | `supervalu:1999065000` | 3 | agent-sprint: wrong brand (Pepsi Strawberries 'N' Cream) |
| supervalu | `supervalu:1999114000` | 3 | agent-sprint: wrong brand (Pepsi Strawberries 'N' Cream) |
| supervalu | `supervalu:1999117000` | 3 | agent-sprint: wrong brand (Pepsi Strawberries 'N' Cream) |
| supervalu | `supervalu:2002875000` | 3 | agent-sprint: wrong brand (Dr Pepper) |
| supervalu | `supervalu:2002876000` | 3 | agent-sprint: wrong brand (Dr Pepper) |
| supervalu | `supervalu:2014995000` | 3 | agent-sprint: wrong brand (Jack Daniel's & Coca-Cola, alcohol) |

## Variant-ambiguous (Feilim batch, never auto-applied)

| retailer | candidate | cells | reasons |
|---|---|---|---|
| supervalu | `supervalu:1002516002` | 3 | agent-sprint: 4-pack cans, wrong format \| agent-sprint: 4-pack multipack |
| supervalu | `supervalu:1005247003` | 2 | agent-sprint: single 2L bottle, wrong pack count and size \| agent-sprint: wrong size 2L |
| supervalu | `supervalu:1008586001` | 3 | agent-sprint: wrong size (6x1.5L bottles) \| agent-sprint: wrong size or pack count, need single Evian 1L |
| supervalu | `supervalu:1008608001` | 2 | agent-sprint: wrong size or pack count, need single Evian 1L \| agent-sprint: wrong size or pack count, need single Evian 50 |
| supervalu | `supervalu:1008703005` | 3 | agent-sprint: 2L bottle, wrong format and size \| agent-sprint: wrong size 2L bottle, need 1.5L |
| supervalu | `supervalu:1009004003` | 3 | agent-sprint: 2L bottle, wrong format and size |
| supervalu | `supervalu:1009004004` | 3 | agent-sprint: wrong sub-variant (Zero Caffeine), 2L bottle |
| supervalu | `supervalu:1009005007` | 3 | agent-sprint: wrong sub-variant (Cherry), 500ml bottle |
| supervalu | `supervalu:1009005008` | 3 | agent-sprint: 500ml bottle, need 1.5L, wrong size \| agent-sprint: 500ml bottle, wrong size |
| supervalu | `supervalu:1009005009` | 3 | agent-sprint: wrong sub-variant (Caffeine Free), 500ml bottl |
| supervalu | `supervalu:1009106005` | 3 | agent-sprint: 2x1L twin pack, wrong format |
| supervalu | `supervalu:1009113005` | 2 | agent-sprint: 6-pack cans, wrong format \| agent-sprint: 6-pack multipack |
| supervalu | `supervalu:1009130003` | 3 | agent-sprint: 330ml can, wrong format and size \| agent-sprint: single can, need 24-pack |
| supervalu | `supervalu:1015836005` | 2 | agent-sprint: 330ml can, wrong format \| agent-sprint: single can, need 6-pack |
| supervalu | `supervalu:1016995006` | 3 | agent-sprint: 500ml bottle, wrong format and size \| agent-sprint: wrong size 500ml bottle, need 1.5L |
| supervalu | `supervalu:1018162012` | 2 | agent-sprint: 500ml bottle, wrong format and size \| agent-sprint: wrong size 500ml bottle |
| supervalu | `supervalu:1018162013` | 2 | agent-sprint: wrong flavour (Lemon) \| agent-sprint: wrong flavour (Lemon) and size 500ml |
| supervalu | `supervalu:1018162014` | 3 | agent-sprint: wrong size or pack count, need Fanta Zero 330m \| agent-sprint: wrong variant (Zero Sugar) and format (500ml b |
| supervalu | `supervalu:1019222003` | 3 | agent-sprint: single 2L bottle, wrong pack count and size \| agent-sprint: wrong size or pack count, need single Ballygow |
| supervalu | `supervalu:1019614003` | 3 | agent-sprint: 2x1L bottle twin pack, wrong format \| agent-sprint: 2x1L twin pack, wrong pack count |
| supervalu | `supervalu:1019740001` | 3 | agent-sprint: single 1L bottle, wrong pack count and size \| agent-sprint: wrong size or pack count, need single Ballygow |
| supervalu | `supervalu:1021611004` | 3 | agent-sprint: wrong line (Energy) and size 380ml \| agent-sprint: wrong size 380ml |
| supervalu | `supervalu:1022603003` | 3 | agent-sprint: 12-pack cans, wrong format \| agent-sprint: 12-pack multipack |
| supervalu | `supervalu:1023917003` | 3 | agent-sprint: 2L bottle, wrong format and size \| agent-sprint: wrong size 2L bottle, need 1.5L |
| supervalu | `supervalu:1024083002` | 3 | agent-sprint: 12-pack cans, wrong format \| agent-sprint: 12-pack multipack, wrong pack count |
| supervalu | `supervalu:1024806002` | 3 | agent-sprint: single 1.5L bottle, wrong pack count and size \| agent-sprint: wrong size or pack count, need single Evian 1L |
| supervalu | `supervalu:1024817004` | 3 | agent-sprint: Cherry 750ml bottle, wrong variant and format \| agent-sprint: wrong variant (Cherry) and format (750ml bottl |
| supervalu | `supervalu:1024817005` | 3 | agent-sprint: 750ml bottle, wrong format and size \| agent-sprint: wrong size 750ml bottle, need 1.5L |
| supervalu | `supervalu:1024876001` | 3 | agent-sprint: single 750ml bottle, wrong pack count and size \| agent-sprint: wrong size or pack count, need single Evian 1L |
| supervalu | `supervalu:1026637002` | 2 | agent-sprint: wrong size 500ml (and Light variant) \| agent-sprint: wrong variant (Light) |
| supervalu | `supervalu:1068606002` | 3 | agent-sprint: multipack 6x380ml \| agent-sprint: wrong line (Energy), multipack 6x380ml |
| supervalu | `supervalu:1160633003` | 3 | agent-sprint: wrong line (Energy) and size 900ml \| agent-sprint: wrong size 900ml |
| supervalu | `supervalu:1210566001` | 3 | agent-sprint: 1.25L bottle, wrong format and size \| agent-sprint: wrong size 1.25L bottle, need 1.5L |
| supervalu | `supervalu:1222815001` | 3 | agent-sprint: single can, need 24-pack, wrong pack count \| agent-sprint: single can, need 6-pack, wrong pack count |
| supervalu | `supervalu:1228819009` | 3 | agent-sprint: wrong product line (Lucozade Sport) and size 7 \| agent-sprint: wrong size 750ml |
| supervalu | `supervalu:1249952001` | 3 | agent-sprint: single 750ml bottle, wrong pack count and size \| agent-sprint: wrong size or pack count, need single Ballygow |
| supervalu | `supervalu:1264166003` | 4 | agent-sprint: 6x500ml pack, wrong pack count and size \| agent-sprint: still water 6-pack, wrong variant |
| supervalu | `supervalu:1270031003` | 2 | agent-sprint: 1L bottle, wrong format and size \| agent-sprint: wrong size 1L bottle |
| supervalu | `supervalu:1284232007` | 3 | agent-sprint: 8-pack cans, wrong format \| agent-sprint: 8-pack multipack, wrong pack count |
| supervalu | `supervalu:1284236006` | 3 | agent-sprint: 8-pack, need 24-pack, wrong pack count \| agent-sprint: 8-pack, need 6-pack, wrong pack count |
| supervalu | `supervalu:1316170002` | 3 | agent-sprint: wrong sub-variant (Zero Caffeine), 4-pack |
| supervalu | `supervalu:1316170003` | 3 | agent-sprint: 4-pack, need 24-pack, wrong pack count \| agent-sprint: 4-pack, need 6-pack, wrong pack count |
| supervalu | `supervalu:1376362001` | 2 | agent-sprint: 4-pack cans, wrong format \| agent-sprint: 4-pack multipack |
| supervalu | `supervalu:1378440003` | 3 | agent-sprint: 1L bottle, need 1.5L, wrong size \| agent-sprint: 1L bottle, wrong size |
| supervalu | `supervalu:1378440004` | 3 | agent-sprint: 1L bottle, wrong size \| agent-sprint: wrong size 1L bottle, need 1.5L |
| supervalu | `supervalu:1380426001` | 3 | agent-sprint: 12-pack, need 24-pack, wrong pack count \| agent-sprint: 12-pack, need 6-pack, wrong pack count |
| supervalu | `supervalu:1433076000` | 2 | agent-sprint: wrong size 300ml |
| supervalu | `supervalu:1433076003` | 2 | agent-sprint: wrong size 300ml |
| supervalu | `supervalu:1438956002` | 3 | agent-sprint: Cherry 2L bottle, wrong variant and format \| agent-sprint: wrong variant (Cherry) and format (2L bottle) |
| supervalu | `supervalu:1451138000` | 2 | agent-sprint: wrong format (concentrate) and size 850ml |
| supervalu | `supervalu:1487145005` | 3 | agent-sprint: wrong size or pack count, need Fanta Zero 330m \| agent-sprint: wrong variant (Zero) and size 1.75L |
| supervalu | `supervalu:1487145006` | 2 | agent-sprint: wrong flavour (Lemon) and size 1.75L |
| supervalu | `supervalu:1487145008` | 2 | agent-sprint: wrong size 1.75L bottle \| agent-sprint: wrong size 1.75L bottle, need 1.5L |
| supervalu | `supervalu:1497121001` | 4 | agent-sprint: sports bottle 6x750ml, wrong size \| agent-sprint: sports bottle 6x750ml, wrong size and pack |
| supervalu | `supervalu:1514538000` | 2 | agent-sprint: wrong format/variant, need Kids orange juice 4 \| agent-sprint: wrong size 1.35L |
| supervalu | `supervalu:1514539000` | 2 | agent-sprint: wrong format/variant, need Kids orange juice 4 \| agent-sprint: wrong size 1.35L |
| supervalu | `supervalu:1514699000` | 3 | agent-sprint: wrong product line (Sport Sparkling) \| agent-sprint: wrong variant (Sparkling) |
| supervalu | `supervalu:1560812000` | 3 | agent-sprint: multipack 8x380ml \| agent-sprint: wrong line (Energy), multipack 8x380ml |
| supervalu | `supervalu:1591173008` | 2 | agent-sprint: single bottle, wrong pack count \| agent-sprint: wrong product line (Lucozade Sport) |
| supervalu | `supervalu:1601285000` | 3 | agent-sprint: wrong format/variant, need Kids orange juice 4 \| agent-sprint: wrong product (Apple Juice Carafe) |
| supervalu | `supervalu:1603099001` | 3 | agent-sprint: wrong sub-variant (Cherry), 2L bottle |
| supervalu | `supervalu:1684889002` | 3 | agent-sprint: multipack 4x380ml \| agent-sprint: wrong line (Energy), multipack 4x380ml |
| supervalu | `supervalu:1684889003` | 2 | agent-sprint: 4-pack multipack, need single bottle \| agent-sprint: wrong product line (Sport) and pack count (4-p |
| supervalu | `supervalu:1697214002` | 2 | agent-sprint: wrong format/variant, need Kids orange juice 4 \| agent-sprint: wrong variant (Orange Mango & Pineapple Kids 6 |
| supervalu | `supervalu:1702753003` | 2 | agent-sprint: wrong variant (Lemon & Lime) and size 750ml \| agent-sprint: wrong variant (Touch of Fruit Lemon & Lime) an |
| supervalu | `supervalu:1732489002` | 2 | agent-sprint: 12-pack cans, wrong format \| agent-sprint: 12-pack multipack |
| supervalu | `supervalu:1732489003` | 3 | agent-sprint: wrong size or pack count, need Fanta Zero 330m \| agent-sprint: wrong variant (Zero) and format (12-pack cans) |
| supervalu | `supervalu:1740360002` | 2 | agent-sprint: wrong line (Energy), single bottle \| agent-sprint: wrong line (Energy, not Sport) |
| supervalu | `supervalu:1790666001` | 3 | agent-sprint: wrong sub-variant (Zero Caffeine), 12-pack |
| supervalu | `supervalu:1803405002` | 2 | agent-sprint: single 1.5L bottle, wrong pack count and size \| agent-sprint: wrong size 1.5L |
| supervalu | `supervalu:1803407002` | 2 | agent-sprint: single 1.5L bottle, wrong pack count and size \| agent-sprint: wrong size or pack count, need single Ballygow |
| supervalu | `supervalu:1809412001` | 3 | agent-sprint: 4x1.5L pack, wrong pack count \| agent-sprint: 4x1.5L pack, wrong size |
| supervalu | `supervalu:1809514001` | 2 | agent-sprint: 4x1.5L pack, wrong pack count and size \| agent-sprint: 4x1.5L pack, wrong size |
| supervalu | `supervalu:1822193000` | 2 | agent-sprint: wrong format/variant, need Kids orange juice 4 \| agent-sprint: wrong size 330ml |
| supervalu | `supervalu:1825245000` | 2 | agent-sprint: wrong format/variant, need Kids orange juice 4 \| agent-sprint: wrong size 330ml |
| supervalu | `supervalu:1919554000` | 2 | agent-sprint: multipack 4x500ml \| agent-sprint: wrong variant (Zero Sugar) and multipack 4x500 |
| supervalu | `supervalu:1939884000` | 3 | agent-sprint: 18-pack cans, wrong format \| agent-sprint: 18-pack multipack |
| supervalu | `supervalu:1939888000` | 3 | agent-sprint: 330ml can, wrong format \| agent-sprint: duplicate Pepsi Max 330ml can listing at €1.35 |
| supervalu | `supervalu:1939955000` | 3 | agent-sprint: Cherry single can, wrong variant \| agent-sprint: wrong variant (Cherry) |
| supervalu | `supervalu:1940756000` | 3 | agent-sprint: 8-pack cans, wrong format \| agent-sprint: 8-pack multipack |
| supervalu | `supervalu:1949641000` | 3 | agent-sprint: 4-pack cans, wrong format \| agent-sprint: 4-pack multipack, wrong pack count |
| supervalu | `supervalu:1950493000` | 2 | agent-sprint: wrong variant (Hint of Fruits Orange) \| agent-sprint: wrong variant (Hint of Fruits Orange), single  |
| supervalu | `supervalu:1950502000` | 2 | agent-sprint: wrong variant (Hint of Fruits Summer Fruits) \| agent-sprint: wrong variant (Hint of Fruits Summer Fruits),  |
| supervalu | `supervalu:1951867000` | 2 | agent-sprint: wrong variant (Hint of Fruits Strawberry) \| agent-sprint: wrong variant (Hint of Fruits Strawberry), sin |
| supervalu | `supervalu:1987173001` | 2 | agent-sprint: wrong variant (Lemon & Lime) and pack count (6 \| agent-sprint: wrong variant (Touch of Fruit Lemon & Lime), 6 |
| supervalu | `supervalu:2003091000` | 3 | agent-sprint: wrong variant (Cherry) and format (12-pack can \| agent-sprint: wrong variant (Cherry) and pack count (12-pack |
| supervalu | `supervalu:2003101000` | 3 | agent-sprint: wrong variant (Cherry) and format (330ml can) \| agent-sprint: wrong variant (Cherry) and pack count (single  |
| supervalu | `supervalu:2012091000` | 3 | agent-sprint: need 1.5L, wrong size 500ml Super Can \| agent-sprint: wrong size 500ml Super Can |
