/**
 * Offline cache payload — pure helpers (spec §5, ticket 06 decision).
 *
 * The cache stores exactly one thing: the raw JSON of the last successful
 * `GET /consumer/feed` response plus the instant it was fetched. Real API
 * data only — there is no code path that can put a synthetic or demo price
 * into the cache, so a cached render is as truthful as a live one. The next
 * successful fetch replaces the payload wholesale; nothing is ever merged.
 *
 * Keyed once for the whole feed: both screens consume the same
 * `/consumer/feed` payload, so one snapshot serves catalog and comparison
 * (ticket 06's "per screen-data kind" split collapses to one kind).
 */
import type { ConsumerFeed } from './feed';

export const CACHE_KEY = 'consumer-feed-cache.v1';

export interface CachedFeed {
  feed: ConsumerFeed;
  /** ISO-8601 instant of the successful fetch that produced this payload. */
  fetchedAt: string;
}

/** Defensive validation — the cache is untrusted local data. */
function isConsumerFeed(value: unknown): value is ConsumerFeed {
  return (
    typeof value === 'object' &&
    value !== null &&
    Array.isArray((value as ConsumerFeed).packs)
  );
}

/**
 * Parse stored cache text. Returns null when absent, corrupt, or shaped
 * wrong — every null path degrades to the honest never-fetched state.
 */
export function parseCache(text: string | null): CachedFeed | null {
  if (text == null) return null;
  try {
    const parsed: unknown = JSON.parse(text);
    if (typeof parsed !== 'object' || parsed === null) return null;
    const { feed, fetchedAt } = parsed as Record<string, unknown>;
    if (!isConsumerFeed(feed) || typeof fetchedAt !== 'string') return null;
    if (Number.isNaN(new Date(fetchedAt).getTime())) return null;
    return { feed, fetchedAt };
  } catch {
    return null;
  }
}

/** The stored form: the raw response plus its fetch timestamp, verbatim. */
export function serializeCache(feed: ConsumerFeed, fetchedAt: string): string {
  return JSON.stringify({ feed, fetchedAt });
}
