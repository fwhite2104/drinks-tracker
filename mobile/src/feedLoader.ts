/**
 * Feed loader — the spec §5 offline policy as a framework-free state machine.
 *
 * Policy (ticket 06 decision, spec §5, verbatim contract):
 * - Cached last-successful-fetch renders immediately, labelled
 *   "as of <fetch date>"; a background refresh runs on launch.
 * - Newest fetch wins: a successful refresh replaces the cache (and the
 *   screen) wholesale — old and new payloads are never merged.
 * - Failed refresh with cache present → non-blocking
 *   "couldn't refresh — showing prices as of <date>" banner; the cached
 *   data stays on screen untouched.
 * - Never-fetched failure → honest error state with retry.
 * - No synthetic prices in any state: the cache holds only real API
 *   responses, and every failure path keeps or drops data — never invents.
 *
 * Deliberately React-free and AsyncStorage-free (both are injected) so the
 * whole policy is exercisable in plain Node against the live or a dead API.
 */
import { formatDate, type ConsumerFeed } from './feed';

export type FeedState =
  /** Cold start, no cache: first fetch in flight. */
  | { kind: 'loading' }
  /** Cold start failed and nothing was ever fetched — honest error + retry. */
  | { kind: 'error'; message: string }
  | {
      kind: 'ready';
      feed: ConsumerFeed;
      /** Fetch date of the payload on screen — render as "as of <date>". */
      asOf: string;
      /** Non-blocking banner text after a failed refresh, else null. */
      banner: string | null;
      /** A refresh is in flight (background or pull-to-refresh). */
      refreshing: boolean;
    };

export interface FeedLoaderDeps {
  readCache(): Promise<{ feed: ConsumerFeed; fetchedAt: string } | null>;
  writeCache(cache: { feed: ConsumerFeed; fetchedAt: string }): Promise<void>;
  fetchFeed(): Promise<ConsumerFeed>;
  now(): number;
}

const BANNER_PREFIX = "couldn't refresh — showing prices as of ";

export function bannerFor(fetchedAt: string): string {
  return `${BANNER_PREFIX}${formatDate(fetchedAt)}`;
}

export class FeedLoader {
  private state: FeedState = { kind: 'loading' };
  /** Monotonic token; a newer start/refresh/stop supersedes in-flight work. */
  private run = 0;
  private started = false;

  private readonly deps: FeedLoaderDeps;
  private readonly listener: (state: FeedState) => void;

  constructor(deps: FeedLoaderDeps, listener: (state: FeedState) => void) {
    this.deps = deps;
    this.listener = listener;
  }

  getState(): FeedState {
    return this.state;
  }

  private setState(state: FeedState): void {
    this.state = state;
    this.listener(state);
  }

  /**
   * Mount: render the cached last-successful-fetch immediately (if any),
   * then always refresh from the API in the background.
   */
  async start(): Promise<void> {
    if (this.started) return;
    this.started = true;
    const run = ++this.run;
    const cached = await this.deps.readCache();
    if (run !== this.run) return;
    if (cached) {
      this.setState({
        kind: 'ready',
        feed: cached.feed,
        asOf: cached.fetchedAt,
        banner: null,
        refreshing: true,
      });
    } else {
      this.setState({ kind: 'loading' });
    }
    void this.refreshFeed(run);
  }

  /**
   * User-initiated refresh or cold-error retry. From `ready` it keeps the
   * current payload on screen while re-fetching (newest wins on success);
   * from `error` it re-enters loading for the honest retry path.
   */
  refresh(): void {
    const run = ++this.run;
    if (this.state.kind === 'ready') {
      this.setState({ ...this.state, banner: null, refreshing: true });
    } else if (this.state.kind === 'error') {
      this.setState({ kind: 'loading' });
    }
    void this.refreshFeed(run);
  }

  /** Unmount: drop in-flight work so no state arrives after the screen dies. */
  stop(): void {
    this.run += 1;
  }

  private async refreshFeed(run: number): Promise<void> {
    try {
      const feed = await this.deps.fetchFeed();
      if (run !== this.run) return; // a newer fetch already won
      const fetchedAt = new Date(this.deps.now()).toISOString();
      await this.deps.writeCache({ feed, fetchedAt });
      if (run !== this.run) return;
      // Whole-payload replace: the new fetch wins outright, never a merge.
      this.setState({
        kind: 'ready',
        feed,
        asOf: fetchedAt,
        banner: null,
        refreshing: false,
      });
    } catch (error) {
      if (run !== this.run) return;
      if (this.state.kind === 'ready') {
        // Cache present → non-blocking banner; cached data stays verbatim.
        this.setState({
          ...this.state,
          refreshing: false,
          banner: bannerFor(this.state.asOf),
        });
      } else {
        // Never fetched → honest error screen with retry.
        this.setState({
          kind: 'error',
          message: error instanceof Error ? error.message : String(error),
        });
      }
    }
  }
}
