/**
 * Route map — the wiring tickets 12 (catalog) and 13 (comparison) share.
 * Comparison is a placeholder until ticket 13 fills it (spec §3.2).
 */
export type RootStackParamList = {
  Catalog: undefined;
  Comparison: { catalog_id: string; name: string; brand: string } | undefined;
};
