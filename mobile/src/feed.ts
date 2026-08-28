/**
 * Consumer feed data layer — types, fetching, and pure catalog helpers.
 *
 * The app never re-derives the §4 state machine: every retailer slot is
 * consumed verbatim from `GET /consumer/feed` (server-owned semantics via
 * `_consumer_cell`). Money stays as decimal strings end-to-end: prices are compared as exact
 * fixed-point decimals (never IEEE doubles) and every displayed price is
 * the server's original string verbatim.
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
 * Exact fixed-point comparison of two decimal strings ("2.35" vs "10.00")
 * without converting to IEEE doubles — spec §4 money purity: money is a
 * decimal string and the app must not re-derive it through floats.
 * Handles multi-euro integer parts and any fraction length. Returns null
 * when either operand is not a plain (optionally signed) decimal number.
 */
export function compareDecimal(a: string, b: string): number | null {
  const left = parseDecimal(a);
  const right = parseDecimal(b);
  if (left == null || right == null) return null;
  const [signA, intA, fracA] = left;
  const [signB, intB, fracB] = right;

  // Compare magnitudes as digit strings: strip insignificant leading zeros,
  // then longer integer part wins, then lexicographic (same length), then
  // right-padded fraction comparison.
  const magA = intA.replace(/^0+(?=\d)/, '');
  const magB = intB.replace(/^0+(?=\d)/, '');
  const width = Math.max(fracA.length, fracB.length);
  const fA = fracA.padEnd(width, '0');
  const fB = fracB.padEnd(width, '0');
  const magnitude =
    magA.length !== magB.length
      ? magA.length < magB.length
        ? -1
        : 1
      : magA === magB
        ? fA === fB
          ? 0
          : fA < fB
            ? -1
            : 1
        : magA < magB
          ? -1
          : 1;

  if (signA !== signB) return signA === '-' ? -1 : 1;
  return signA === '-' ? -magnitude : magnitude;
}

/** Splits a decimal string into [sign, integer digits, fraction digits]. */
function parseDecimal(s: string): [string, string, string] | null {
  const match = /^(-)?(\d+)(?:\.(\d*))?$/.exec(s.trim());
  if (match == null) return null;
  return [match[1] ?? '+', match[2], match[3] ?? ''];
}

/**
 * Cheapest current (observed) Displayed Price across retailers, returned
 * as the server's original decimal string verbatim (never re-formatted).
 * Comparison is exact fixed-point (see `compareDecimal`), so "10.00" vs
 * "2.35" picks "2.35" regardless of string or float ordering. Null when no
 * retailer has an observed price — the card then shows the honest
 * "no prices yet" state.
 */
export function cheapestPrice(pack: FeedPack): string | null {
  let cheapest: string | null = null;
  for (const cell of pack.retailers) {
    if (cell.state !== 'observed' || cell.displayed_price == null) continue;
    if (cheapest == null) {
      if (compareDecimal(cell.displayed_price, cell.displayed_price) != null) {
        cheapest = cell.displayed_price;
      }
      continue;
    }
    const cmp = compareDecimal(cell.displayed_price, cheapest);
    if (cmp != null && cmp < 0) cheapest = cell.displayed_price;
  }
  return cheapest;
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
  // Exact decimal-string comparison — never floats (spec §4 money purity).
  // Cells here always have a non-null displayed_price; unparseable values
  // (never expected from the server) compare as equal and keep their order.
  priced.sort(
    (a, b) =>
      compareDecimal(a.displayed_price ?? '', b.displayed_price ?? '') ?? 0
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
 * ~24 packs: brands are walked alphabetically (locale-aware) until the
 * visible budget is crossed, so each scroll step adds one more chunk.
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
  // Map iteration preserves API insertion order — sort the brand keys
  // explicitly so sections really are alphabetical (spec §3.1 prototype).
  const brands = [...byBrand.keys()].sort((a, b) => a.localeCompare(b));
  for (const brand of brands) {
    const brandPacks = byBrand.get(brand)!;
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
