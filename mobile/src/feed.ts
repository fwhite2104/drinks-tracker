/**
 * Consumer feed data layer — types, fetching, and pure catalog helpers.
 *
 * The app never re-derives the §4 state machine: every retailer slot is
 * consumed verbatim from `GET /consumer/feed` (server-owned semantics via
 * `_consumer_cell`). Money stays as decimal strings end-to-end; the only
 * numeric use is picking the cheapest observed price per pack.
 */

const API_BASE_URL = process.env.EXPO_PUBLIC_API_BASE_URL;

/** §4 five-state machine, verbatim from the server — never re-derived here. */
export type CellState =
  | 'observed'
  | 'last_seen'
  | 'awaiting_price'
  | 'temporarily_unavailable'
  | 'not_available';

export interface RetailerCell {
  retailer: string;
  display_name: string;
  state: CellState;
  /** Server-owned §4 label — rendered verbatim, never re-derived. */
  label: string;
  displayed_price: string | null;
  clubcard_price: string | null;
  drs_deposit: string | null;
  component_unit_price: string | null;
  source_scope: string | null;
  observed_at: string | null;
  /** Present only on `last_seen` cells — never carries the old price. */
  last_seen_at?: string | null;
  currency: string;
  /** Server-marked cheapest observed slot (informational; ordering is by price). */
  is_best?: boolean;
}

export interface FeedPack {
  catalog_id: string;
  name: string;
  brand: string;
  variant: string;
  pack_count: number;
  unit_size_ml: number;
  package_type: string;
  pack_label: string;
  retailers: RetailerCell[];
}

export interface ConsumerFeed {
  generated_at: string;
  packs: FeedPack[];
}

/**
 * Fetch the consumer feed. Throws with honest, human-readable messages for
 * every failure mode (unset base URL, non-200, malformed response) — the
 * screen renders them verbatim with a Retry (spec §5 spirit).
 */
export async function fetchConsumerFeed(): Promise<ConsumerFeed> {
  if (!API_BASE_URL) {
    throw new Error(
      'EXPO_PUBLIC_API_BASE_URL is not set — copy .env.example to .env and restart the dev server.'
    );
  }
  const response = await fetch(`${API_BASE_URL}/consumer/feed`);
  if (!response.ok) {
    throw new Error(`HTTP ${response.status} from ${API_BASE_URL}/consumer/feed`);
  }
  const feed = (await response.json()) as ConsumerFeed;
  if (!Array.isArray(feed.packs)) {
    throw new Error('Response did not look like the consumer feed');
  }
  return feed;
}

/** "8×330ml · can" — multipacks show count, singles just the size. */
export function packMeta(pack: FeedPack): string {
  const size =
    pack.pack_count > 1
      ? `${pack.pack_count}×${pack.unit_size_ml}ml`
      : `${pack.unit_size_ml}ml`;
  return [size, pack.package_type].join(' · ');
}

/**
 * Cheapest current (observed) Displayed Price across retailers, as the
 * server-supplied decimal string. Null when no retailer has an observed
 * price — the card then shows the honest "no prices yet" state.
 */
export function cheapestPrice(pack: FeedPack): string | null {
  let cheapest: number | null = null;
  for (const cell of pack.retailers) {
    if (cell.state !== 'observed' || cell.displayed_price == null) continue;
    const price = Number(cell.displayed_price);
    if (!Number.isFinite(price)) continue;
    if (cheapest == null || price < cheapest) cheapest = price;
  }
  return cheapest == null ? null : cheapest.toFixed(2);
}

const MONTHS = [
  'Jan', 'Feb', 'Mar', 'Apr', 'May', 'Jun',
  'Jul', 'Aug', 'Sep', 'Oct', 'Nov', 'Dec',
];

/** "27 Aug 2026" from an ISO-8601 UTC instant (manual, Hermes-safe). */
export function formatDate(iso: string): string {
  const d = new Date(iso);
  if (Number.isNaN(d.getTime())) return iso;
  return `${d.getUTCDate()} ${MONTHS[d.getUTCMonth()]} ${d.getUTCFullYear()}`;
}

/** True when the observation is older than 7 days (spec §3.2 staleness note). */
export function isStale(iso: string, now: number = Date.now()): boolean {
  const t = new Date(iso).getTime();
  return Number.isFinite(t) && now - t > 7 * 24 * 60 * 60 * 1000;
}

/**
 * Cheapest-first retailer order for the comparison screen (spec §2/§3.2):
 * observed cells sorted ascending by Displayed Price, all non-observed
 * states after them in server order. Clubcard never enters the comparison —
 * only `displayed_price` is read, so a member price can never promote a
 * retailer above a cheaper shelf price.
 */
export function orderedCells(pack: FeedPack): RetailerCell[] {
  const priced: RetailerCell[] = [];
  const rest: RetailerCell[] = [];
  for (const cell of pack.retailers) {
    if (cell.state === 'observed' && cell.displayed_price != null) {
      priced.push(cell);
    } else {
      rest.push(cell);
    }
  }
  priced.sort(
    (a, b) => Number(a.displayed_price) - Number(b.displayed_price)
  );
  return [...priced, ...rest];
}

/** Case-insensitive substring match across pack name and brand. */
export function matchesQuery(pack: FeedPack, query: string): boolean {
  const q = query.trim().toLowerCase();
  if (!q) return true;
  return pack.name.toLowerCase().includes(q) || pack.brand.toLowerCase().includes(q);
}

/** Lazy-loading chunk size on scroll (spec §3.1 acceptance criterion). */
export const CHUNK_SIZE = 24;

export interface CatalogSection {
  key: string;
  title: string;
  /** Count shown in the sticky header, e.g. "COCA-COLA · 7". */
  count: number;
  /** Packs pre-paired for the two-column card grid. */
  data: [FeedPack, FeedPack | null][];
}

function pairRows(packs: FeedPack[]): [FeedPack, FeedPack | null][] {
  const rows: [FeedPack, FeedPack | null][] = [];
  for (let i = 0; i < packs.length; i += 2) {
    rows.push([packs[i], packs[i + 1] ?? null]);
  }
  return rows;
}

/**
 * Brand-grouped sections for the browse view, revealed in lazy chunks of
 * ~24 packs: brands are walked alphabetically until the visible budget is
 * crossed, so each scroll step adds one more chunk. Returns null when the
 * budget still covers brands beyond `visibleCount` (i.e. more to load).
 */
export function buildBrowseSections(
  packs: FeedPack[],
  visibleCount: number
): { sections: CatalogSection[]; hasMore: boolean; shownCount: number } {
  const byBrand = new Map<string, FeedPack[]>();
  for (const pack of packs) {
    const list = byBrand.get(pack.brand);
    if (list) list.push(pack);
    else byBrand.set(pack.brand, [pack]);
  }

  const sections: CatalogSection[] = [];
  let acc = 0;
  let hasMore = false;
  for (const [brand, brandPacks] of byBrand) {
    if (acc >= visibleCount) {
      hasMore = true;
      break;
    }
    sections.push({
      key: brand,
      title: brand,
      count: brandPacks.length,
      data: pairRows(brandPacks),
    });
    acc += brandPacks.length;
  }
  return { sections, hasMore, shownCount: acc };
}

/** Flat search results (spec §3.1: search switches to flat filtered results). */
export function buildSearchSections(
  packs: FeedPack[],
  query: string,
  visibleCount: number
): { sections: CatalogSection[]; hasMore: boolean; shownCount: number } {
  const all = packs.filter((pack) => matchesQuery(pack, query));
  const visible = all.slice(0, visibleCount);
  return {
    sections: [
      { key: 'results', title: 'Results', count: all.length, data: pairRows(visible) },
    ],
    hasMore: all.length > visible.length,
    shownCount: visible.length,
  };
}
