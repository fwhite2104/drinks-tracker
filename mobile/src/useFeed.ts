/**
 * React binding for the spec §5 feed policy (see feedLoader.ts).
 *
 * Each screen mounts its own loader: on mount the cached last-successful
 * fetch renders immediately and a background refresh runs; failures with
 * cache become a non-blocking banner, failures without cache become the
 * honest error state with retry.
 */
import { useEffect, useRef, useState } from 'react';

import { fetchConsumerFeed } from './feed';
import { FeedLoader, type FeedState } from './feedLoader';
import { readCachedFeed, writeCachedFeed } from './feedStorage';

export function useFeed(): { state: FeedState; refresh: () => void } {
  const [state, setState] = useState<FeedState>({ kind: 'loading' });
  const loaderRef = useRef<FeedLoader | null>(null);
  if (loaderRef.current === null) {
    loaderRef.current = new FeedLoader(
      {
        readCache: readCachedFeed,
        writeCache: ({ feed, fetchedAt }) => writeCachedFeed(feed, fetchedAt),
        fetchFeed: fetchConsumerFeed,
        now: Date.now,
      },
      setState
    );
  }

  useEffect(() => {
    const loader = loaderRef.current;
    if (loader) void loader.start();
    return () => loader?.stop();
  }, []);

  return { state, refresh: () => loaderRef.current?.refresh() };
}
