/**
 * AsyncStorage backing for the offline cache (spec §5).
 *
 * Minimal key-value persistence of the last successful fetch. Writes are
 * best-effort: a full disk or storage error must never take down a screen
 * that is otherwise showing perfectly good data.
 */
import AsyncStorage from '@react-native-async-storage/async-storage';

import { CACHE_KEY, parseCache, serializeCache, type CachedFeed } from './feedCache';
import type { ConsumerFeed } from './feed';

export async function readCachedFeed(): Promise<CachedFeed | null> {
  try {
    return parseCache(await AsyncStorage.getItem(CACHE_KEY));
  } catch {
    return null;
  }
}

export async function writeCachedFeed(
  feed: ConsumerFeed,
  fetchedAt: string
): Promise<void> {
  try {
    await AsyncStorage.setItem(CACHE_KEY, serializeCache(feed, fetchedAt));
  } catch {
    // Cache write failed — the in-memory feed on screen stays authoritative.
  }
}
