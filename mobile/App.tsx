/**
 * Stub catalog screen — proves endpoint → app connectivity (spec §8, ticket 11).
 *
 * Fetches GET /consumer/feed from the build-time base URL and renders pack
 * names + per-retailer state labels as plain rows. No polish, no comparison
 * UI, no cache (ticket 14 owns offline policy) — just the honest five-state
 * contract surfaced raw, and an honest error state when the feed is
 * unreachable. Never invents prices (spec §5).
 */
import { useCallback, useEffect, useState } from 'react';
import {
  ActivityIndicator,
  Pressable,
  ScrollView,
  StyleSheet,
  Text,
  View,
} from 'react-native';
import { StatusBar } from 'expo-status-bar';

const API_BASE_URL = process.env.EXPO_PUBLIC_API_BASE_URL;

/** §4 five-state machine, verbatim from the server — never re-derived here. */
type CellState =
  | 'observed'
  | 'last_seen'
  | 'awaiting_price'
  | 'temporarily_unavailable'
  | 'not_available';

interface RetailerCell {
  retailer: string;
  display_name: string;
  state: CellState;
  label: string;
  displayed_price: string | null;
}

interface FeedPack {
  catalog_id: string;
  name: string;
  pack_label: string;
  retailers: RetailerCell[];
}

interface ConsumerFeed {
  generated_at: string;
  packs: FeedPack[];
}

type FeedStatus =
  | { kind: 'loading' }
  | { kind: 'loaded'; feed: ConsumerFeed }
  | { kind: 'error'; message: string };

function cellText(cell: RetailerCell): string {
  // Observed slots carry the Displayed Price; the other four states are
  // price-less by contract, so the server label alone is the truth.
  return cell.displayed_price != null
    ? `${cell.display_name}: ${cell.label} — €${cell.displayed_price}`
    : `${cell.display_name}: ${cell.label}`;
}

export default function App() {
  const [status, setStatus] = useState<FeedStatus>({ kind: 'loading' });
  const [attempt, setAttempt] = useState(0);

  const retry = useCallback(() => {
    setStatus({ kind: 'loading' });
    setAttempt((n) => n + 1);
  }, []);

  useEffect(() => {
    let alive = true;

    async function load(): Promise<ConsumerFeed> {
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

    load()
      .then((feed) => {
        if (alive) setStatus({ kind: 'loaded', feed });
      })
      .catch((error: unknown) => {
        if (alive) {
          setStatus({
            kind: 'error',
            message: error instanceof Error ? error.message : String(error),
          });
        }
      });

    return () => {
      alive = false;
    };
  }, [attempt]);

  return (
    <View style={styles.container}>
      <StatusBar style="light" />
      <Text style={styles.header}>Find the cheaper bottle.</Text>
      <Text style={styles.baseUrl}>API: {API_BASE_URL ?? '(not configured)'}</Text>

      {status.kind === 'loading' && (
        <View style={styles.centered}>
          <ActivityIndicator size="large" color="#7FB69E" />
          <Text style={styles.message}>Loading feed…</Text>
        </View>
      )}

      {status.kind === 'error' && (
        <View style={styles.centered}>
          <Text style={styles.errorTitle}>Couldn&apos;t load the feed</Text>
          <Text style={styles.message}>{status.message}</Text>
          <Pressable style={styles.retryButton} onPress={retry}>
            <Text style={styles.retryLabel}>Retry</Text>
          </Pressable>
        </View>
      )}

      {status.kind === 'loaded' && (
        <ScrollView style={styles.list}>
          {status.feed.packs.map((pack) => (
            <View key={pack.catalog_id} style={styles.pack}>
              <Text style={styles.packName}>{pack.name}</Text>
              {pack.retailers.map((cell) => (
                <Text key={cell.retailer} style={styles.cell}>
                  {cellText(cell)}
                </Text>
              ))}
            </View>
          ))}
        </ScrollView>
      )}
    </View>
  );
}

const styles = StyleSheet.create({
  container: {
    flex: 1,
    backgroundColor: '#0B3D2E',
    paddingTop: 60,
    paddingHorizontal: 16,
  },
  header: {
    color: '#F4F1EA',
    fontSize: 24,
    fontWeight: '700',
  },
  baseUrl: {
    color: '#7FB69E',
    fontSize: 12,
    marginBottom: 12,
  },
  centered: {
    alignItems: 'center',
    gap: 12,
    marginTop: 48,
  },
  message: {
    color: '#C9C4B8',
    textAlign: 'center',
  },
  errorTitle: {
    color: '#F4F1EA',
    fontSize: 18,
    fontWeight: '600',
  },
  retryButton: {
    backgroundColor: '#7FB69E',
    borderRadius: 8,
    paddingHorizontal: 24,
    paddingVertical: 10,
  },
  retryLabel: {
    color: '#0B3D2E',
    fontWeight: '700',
  },
  list: {
    marginTop: 8,
  },
  pack: {
    paddingVertical: 10,
    borderBottomWidth: StyleSheet.hairlineWidth,
    borderBottomColor: '#1C5C45',
  },
  packName: {
    color: '#F4F1EA',
    fontSize: 16,
    fontWeight: '600',
  },
  cell: {
    color: '#C9C4B8',
    fontSize: 13,
    paddingLeft: 8,
  },
});
