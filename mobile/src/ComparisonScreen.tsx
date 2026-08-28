/**
 * Exact-Pack Comparison — spec §3.2, information contract §4.
 *
 * Semantics are server-owned: every retailer slot is rendered exactly as
 * `/consumer/feed` delivers it (`observed`, `last_seen`, `awaiting_price`,
 * `temporarily_unavailable`, `not_available`) and is never re-derived here.
 * Size encodes rank — the cheapest `observed` retailer is a hero card, the
 * rest are smaller rows, non-`observed` states are subdued state rows.
 * Ordering is by Displayed Price only; Clubcard never affects it. DRS
 * deposit is always its own ♻ line. Visual language: prototype variant C
 * "Bottle green".
 */
import { useCallback, useEffect, useMemo, useState } from 'react';
import {
  ActivityIndicator,
  Pressable,
  ScrollView,
  StyleSheet,
  Text,
  View,
} from 'react-native';
import { StatusBar } from 'expo-status-bar';
import { useNavigation, useRoute } from '@react-navigation/native';
import type { NativeStackNavigationProp } from '@react-navigation/native-stack';

import {
  fetchConsumerFeed,
  formatDate,
  isStale,
  orderedCells,
  packMeta,
  type ConsumerFeed,
  type FeedPack,
  type RetailerCell,
} from './feed';
import type { RootStackParamList } from './navigation';
import {
  statePreviewPack,
} from './statePreview';

type FeedStatus =
  | { kind: 'loading' }
  | { kind: 'loaded'; feed: ConsumerFeed }
  | { kind: 'error'; message: string };

export default function ComparisonScreen() {
  const navigation = useNavigation<NativeStackNavigationProp<RootStackParamList>>();
  const route = useRoute<{ key: string; name: 'Comparison'; params?: RootStackParamList['Comparison'] }>();
  const [status, setStatus] = useState<FeedStatus>({ kind: 'loading' });
  const [attempt, setAttempt] = useState(0);
  const [previewing, setPreviewing] = useState(false);

  useEffect(() => {
    let alive = true;

    fetchConsumerFeed()
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

  const catalogId = route.params?.catalog_id;
  // Dev-only §4 state preview (ticket 13 acceptance): the fixture replaces
  // the live pack while toggled; null in release builds.
  const previewPack = useMemo(() => statePreviewPack(), []);

  const pack: FeedPack | null = useMemo(() => {
    if (previewing && previewPack) return previewPack;
    if (status.kind !== 'loaded') return null;
    return (
      status.feed.packs.find((candidate) => candidate.catalog_id === catalogId) ??
      null
    );
  }, [status, catalogId, previewing, previewPack]);

  const cells: RetailerCell[] = useMemo(
    () => (pack ? orderedCells(pack) : []),
    [pack]
  );

  const refresh = useCallback(() => {
    setStatus({ kind: 'loading' });
    setAttempt((n) => n + 1);
  }, []);

  return (
    <View style={styles.screen}>
      <StatusBar style="dark" />

      {status.kind === 'loading' && (
        <View style={[styles.centered, styles.fill]}>
          <ActivityIndicator size="large" color="#0B3D2E" />
          <Text style={styles.muted}>Loading feed…</Text>
        </View>
      )}

      {status.kind === 'error' && (
        <View style={[styles.centered, styles.fill]}>
          <Text style={styles.errorTitle}>Couldn&apos;t load the feed</Text>
          <Text style={styles.muted}>{status.message}</Text>
          <Pressable style={styles.retryButton} onPress={refresh}>
            <Text style={styles.retryLabel}>Retry</Text>
          </Pressable>
        </View>
      )}

      {status.kind === 'loaded' && !pack && (
        <View style={[styles.centered, styles.fill]}>
          <Text style={styles.errorTitle}>Pack not in the feed</Text>
          <Text style={styles.muted}>
            It may have been renamed — go back and pick it again.
          </Text>
        </View>
      )}

      {status.kind === 'loaded' && pack && (
        <ScrollView contentContainerStyle={styles.content}>
          <Pressable style={styles.back} onPress={() => navigation.goBack()}>
            <Text style={styles.backLabel}>‹ All packs</Text>
          </Pressable>

          {/* Header: pack name + meta. */}
          <Text style={styles.title}>{pack.name}</Text>
          <Text style={styles.subtitle}>{packMeta(pack)}</Text>

          {cells[0]?.state === 'observed' ? (
            <HeroCard cell={cells[0]} />
          ) : (
            <View style={styles.noPrices}>
              <Text style={styles.noPricesTitle}>No current prices</Text>
              <Text style={styles.noPricesBody}>
                No retailer has an observed price for this pack right now.
                That doesn&apos;t mean it&apos;s out of stock — see the states
                below.
              </Text>
            </View>
          )}

          {/* Remaining observed retailers as smaller rows… */}
          {cells.slice(1).map((cell) =>
            cell.state === 'observed' ? (
              <ObservedRow key={cell.retailer} cell={cell} />
            ) : (
              /* …and non-observed states as subdued state rows. */
              <StateRow key={cell.retailer} cell={cell} />
            )
          )}

          <Text style={styles.note}>
            Shelf prices at collection time; the refundable deposit is always
            shown separately. A missing price never means out of stock, and
            &ldquo;last seen&rdquo; is not discontinued.
          </Text>

          {/* Dev-only: verify all five §4 states on screen. Never in release. */}
          {previewPack && (
            <Pressable
              style={styles.devToggle}
              onPress={() => setPreviewing((on) => !on)}
            >
              <Text style={styles.devToggleLabel}>
                {previewing
                  ? 'DEV: exit §4 state preview'
                  : 'DEV: preview all five §4 states'}
              </Text>
            </Pressable>
          )}
        </ScrollView>
      )}
    </View>
  );
}

/** Large hero card for the cheapest observed retailer (spec §3.2). */
function HeroCard({ cell }: { cell: RetailerCell }) {
  const observedAt = cell.observed_at;
  const stale = observedAt != null && isStale(observedAt);
  return (
    <View style={styles.hero}>
      <Text style={styles.heroTag}>Cheapest now</Text>
      <Text style={styles.heroRetailer}>{cell.display_name}</Text>
      <Text style={styles.heroPrice}>
        <Text style={styles.heroEuro}>€</Text>
        {cell.displayed_price}
      </Text>

      {cell.component_unit_price != null && (
        <Text style={styles.heroSub}>€{cell.component_unit_price} per can</Text>
      )}
      {cell.drs_deposit != null && (
        <Text style={styles.heroSub}>
          ♻ +€{cell.drs_deposit} refundable deposit
        </Text>
      )}

      {cell.clubcard_price != null && (
        <View style={styles.heroClubcardPill}>
          <Text style={styles.clubcardPrice}>Clubcard €{cell.clubcard_price}</Text>
          <Text style={styles.clubcardMember}>member price</Text>
        </View>
      )}

      {observedAt != null && (
        <Text style={styles.heroWhen}>
          Observed {formatDate(observedAt)}
          {stale ? ' · may be out of date' : ''}
        </Text>
      )}
    </View>
  );
}

/** Smaller row for a non-hero observed retailer. */
function ObservedRow({ cell }: { cell: RetailerCell }) {
  const observedAt = cell.observed_at;
  const stale = observedAt != null && isStale(observedAt);
  const meta = [
    cell.component_unit_price != null
      ? `€${cell.component_unit_price} per can`
      : null,
    cell.drs_deposit != null ? `♻ €${cell.drs_deposit} deposit` : null,
    observedAt != null
      ? `Observed ${formatDate(observedAt)}${stale ? ' · may be out of date' : ''}`
      : null,
  ]
    .filter(Boolean)
    .join(' · ');

  return (
    <View style={styles.row}>
      <View style={styles.rowWho}>
        <Text style={styles.rowRetailer}>{cell.display_name}</Text>
        <Text style={styles.rowMeta}>{meta}</Text>
      </View>
      <View style={styles.rowPriceCol}>
        <Text style={styles.rowPrice}>€{cell.displayed_price}</Text>
        {cell.clubcard_price != null && (
          <View style={styles.rowClubcardPill}>
            <Text style={styles.clubcardPriceSmall}>
              Clubcard €{cell.clubcard_price}
            </Text>
            <Text style={styles.clubcardMemberSmall}>member price</Text>
          </View>
        )}
      </View>
    </View>
  );
}

/**
 * Subdued row for a non-`observed` state — the server-owned label rendered
 * verbatim (`Last seen` gains its date; never the old price, which the feed
 * already omits).
 */
function StateRow({ cell }: { cell: RetailerCell }) {
  const stateText =
    cell.state === 'last_seen'
      ? `${cell.label} ${cell.last_seen_at != null ? formatDate(cell.last_seen_at) : ''}`.trim()
      : cell.label;
  return (
    <View style={styles.stateRow}>
      <Text style={styles.stateRetailer}>{cell.display_name}</Text>
      <Text style={styles.stateText}>{stateText}</Text>
    </View>
  );
}

const styles = StyleSheet.create({
  screen: {
    flex: 1,
    backgroundColor: '#F2EFE7',
  },
  fill: {
    flex: 1,
    alignItems: 'center',
    justifyContent: 'center',
    gap: 12,
    padding: 24,
  },
  content: {
    paddingBottom: 32,
  },
  back: {
    alignSelf: 'flex-start',
    paddingVertical: 8,
    paddingRight: 16,
    marginTop: 52,
  },
  backLabel: {
    color: '#0B3D2E',
    fontSize: 14,
    fontWeight: '600',
  },
  title: {
    color: '#0B3D2E',
    fontSize: 22,
    fontWeight: '800',
    letterSpacing: -0.3,
    marginHorizontal: 16,
    marginTop: 2,
  },
  subtitle: {
    color: '#6D7F72',
    fontSize: 12,
    marginHorizontal: 16,
    marginTop: 3,
    marginBottom: 2,
  },

  /* Hero — cheapest observed retailer. */
  hero: {
    backgroundColor: '#0B3D2E',
    borderRadius: 24,
    marginHorizontal: 16,
    marginTop: 12,
    padding: 20,
  },
  heroTag: {
    alignSelf: 'flex-start',
    backgroundColor: '#FFD166',
    color: '#0B3D2E',
    fontSize: 10,
    fontWeight: '800',
    letterSpacing: 0.9,
    textTransform: 'uppercase',
    borderRadius: 999,
    overflow: 'hidden',
    paddingVertical: 4,
    paddingHorizontal: 10,
  },
  heroRetailer: {
    color: '#FFFFFF',
    fontSize: 16,
    fontWeight: '800',
    marginTop: 10,
  },
  heroPrice: {
    color: '#FFFFFF',
    fontSize: 52,
    fontWeight: '800',
    letterSpacing: -1.5,
    lineHeight: 56,
    marginTop: 4,
  },
  heroEuro: {
    fontSize: 22,
    fontWeight: '700',
    letterSpacing: 0,
  },
  heroSub: {
    color: '#BCD6C6',
    fontSize: 12,
    lineHeight: 20,
  },
  heroClubcardPill: {
    alignSelf: 'flex-start',
    backgroundColor: '#FFD166',
    borderRadius: 10,
    paddingVertical: 5,
    paddingHorizontal: 10,
    marginTop: 10,
  },
  clubcardPrice: {
    color: '#0B3D2E',
    fontSize: 11.5,
    fontWeight: '800',
  },
  clubcardMember: {
    color: '#8A6D00',
    fontSize: 8,
    letterSpacing: 0.4,
    textTransform: 'uppercase',
    fontWeight: '700',
  },
  heroWhen: {
    color: '#9DBFAD',
    fontSize: 10.5,
    marginTop: 12,
  },

  /* Smaller observed rows. */
  row: {
    backgroundColor: '#FFFFFF',
    borderRadius: 16,
    marginHorizontal: 16,
    marginTop: 8,
    paddingVertical: 11,
    paddingHorizontal: 15,
    flexDirection: 'row',
    justifyContent: 'space-between',
    alignItems: 'center',
    gap: 12,
    shadowColor: '#0B3D2E',
    shadowOpacity: 0.1,
    shadowRadius: 4,
    shadowOffset: { width: 0, height: 1 },
    elevation: 2,
  },
  rowWho: {
    flexShrink: 1,
  },
  rowRetailer: {
    color: '#0B3D2E',
    fontSize: 13,
    fontWeight: '700',
  },
  rowMeta: {
    color: '#6D7F72',
    fontSize: 10.5,
    marginTop: 2,
    lineHeight: 15,
  },
  rowPriceCol: {
    alignItems: 'flex-end',
    gap: 4,
  },
  rowPrice: {
    color: '#0B3D2E',
    fontSize: 18,
    fontWeight: '800',
  },
  rowClubcardPill: {
    backgroundColor: '#FFD166',
    borderRadius: 7,
    paddingVertical: 3,
    paddingHorizontal: 8,
    alignItems: 'flex-end',
  },
  clubcardPriceSmall: {
    color: '#0B3D2E',
    fontSize: 10.5,
    fontWeight: '800',
  },
  clubcardMemberSmall: {
    color: '#8A6D00',
    fontSize: 7.5,
    letterSpacing: 0.3,
    textTransform: 'uppercase',
    fontWeight: '700',
  },

  /* Subdued state rows. */
  stateRow: {
    backgroundColor: '#E4E9E2',
    borderRadius: 14,
    marginHorizontal: 16,
    marginTop: 8,
    paddingVertical: 11,
    paddingHorizontal: 15,
    flexDirection: 'row',
    justifyContent: 'space-between',
    alignItems: 'center',
    gap: 12,
  },
  stateRetailer: {
    color: '#0B3D2E',
    fontSize: 12.5,
    fontWeight: '700',
    flexShrink: 1,
  },
  stateText: {
    color: '#3F5449',
    fontSize: 12.5,
    textAlign: 'right',
  },

  /* Honest no-current-prices panel (all states non-observed). */
  noPrices: {
    backgroundColor: '#0B3D2E',
    borderRadius: 24,
    marginHorizontal: 16,
    marginTop: 12,
    padding: 20,
  },
  noPricesTitle: {
    color: '#FFFFFF',
    fontSize: 18,
    fontWeight: '800',
  },
  noPricesBody: {
    color: '#BCD6C6',
    fontSize: 12,
    lineHeight: 19,
    marginTop: 6,
  },

  note: {
    color: '#6D7F72',
    fontSize: 11,
    lineHeight: 17,
    marginHorizontal: 16,
    marginTop: 16,
  },

  devToggle: {
    alignSelf: 'center',
    marginTop: 18,
    borderWidth: 1.5,
    borderStyle: 'dashed',
    borderColor: '#0B3D2E',
    borderRadius: 999,
    paddingVertical: 7,
    paddingHorizontal: 14,
  },
  devToggleLabel: {
    color: '#0B3D2E',
    fontSize: 11,
    fontWeight: '700',
  },

  /* Loading / error states. */
  centered: {
    alignItems: 'center',
    justifyContent: 'center',
    gap: 12,
  },
  muted: {
    color: '#3F5449',
    textAlign: 'center',
  },
  errorTitle: {
    color: '#0B3D2E',
    fontSize: 18,
    fontWeight: '700',
  },
  retryButton: {
    backgroundColor: '#0B3D2E',
    borderRadius: 999,
    paddingHorizontal: 24,
    paddingVertical: 10,
  },
  retryLabel: {
    color: '#FFFFFF',
    fontWeight: '700',
  },
});
