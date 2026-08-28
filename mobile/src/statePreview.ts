/**
 * DEV-ONLY state-preview fixture (ticket 13 acceptance criterion: all five
 * §4 states must be visually verifiable without waiting for the feed to
 * produce them).
 *
 * Hard-gated behind `__DEV__`: `statePreviewPack()` returns null in release
 * builds, so the fixture and its toggle can never ship — and §5's
 * "no synthetic prices" rule stays intact in production. All five §4 states
 * get a slot; dates are computed relative to now so the >7-day staleness
 * note is exercisable too.
 */
import type { CellState, FeedPack, RetailerCell } from './feed';

function iso(daysAgo: number): string {
  return new Date(Date.now() - daysAgo * 24 * 60 * 60 * 1000).toISOString();
}

/** One synthetic pack carrying all five §4 states across its five retailers. */
export function statePreviewPack(): FeedPack | null {
  if (!__DEV__) return null;

  const base = {
    source_scope: null as string | null,
    currency: 'EUR',
    is_best: false,
  };
  const cell = (
    retailer: string,
    display_name: string,
    state: CellState,
    extra: Partial<RetailerCell>
  ): RetailerCell => ({
    retailer,
    display_name,
    state,
    label: {
      observed: 'Observed',
      last_seen: 'Last seen',
      awaiting_price: 'Awaiting price',
      temporarily_unavailable: 'Temporarily unavailable',
      not_available: 'Not available',
    }[state],
    displayed_price: null,
    clubcard_price: null,
    drs_deposit: null,
    component_unit_price: null,
    observed_at: null,
    ...base,
    ...extra,
  });

  return {
    catalog_id: '__state_preview__',
    name: 'State Preview — all five §4 states',
    brand: 'DEV PREVIEW',
    variant: 'Fixture',
    pack_count: 8,
    unit_size_ml: 330,
    package_type: 'can',
    pack_label: 'State Preview · 8×330ml can',
    retailers: [
      cell('supervalu', 'SuperValu', 'observed', {
        displayed_price: '4.29',
        drs_deposit: '0.25',
        component_unit_price: '0.54',
        observed_at: iso(9), // >7 days → stale note
        is_best: true,
      }),
      cell('tesco', 'Tesco Ireland', 'observed', {
        displayed_price: '4.85',
        clubcard_price: '3.90', // side-by-side pill; ordering ignores it
        drs_deposit: '0.25',
        component_unit_price: '0.61',
        observed_at: iso(1),
      }),
      cell('dunnes', 'Dunnes Stores', 'last_seen', {
        last_seen_at: iso(21),
        observed_at: iso(21),
      }),
      cell('lidl', 'Lidl Ireland', 'awaiting_price', {}),
      cell('aldi', 'Aldi Ireland', 'temporarily_unavailable', {}),
      cell('extra', 'Dormant Retailer (example)', 'not_available', {}),
    ],
  };
}
