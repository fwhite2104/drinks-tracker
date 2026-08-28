/**
 * Catalog screen — the cold-launch screen (spec §3.1).
 *
 * Search bar first; below it the full catalog as a two-column card grid
 * grouped by brand with sticky section headers, lazily revealed in chunks
 * of ~24 packs on scroll. Search switches to flat filtered results across
 * name/brand. Visual language: prototype variant C "Bottle green".
 * No category grid, no curated subset, no accounts/favourites.
 *
 * Feed access follows spec §5 (useFeed): the cached last-successful fetch
 * renders immediately labelled "as of <fetch date>", a background refresh
 * runs on launch, a failed refresh shows a non-blocking banner over the
 * cached data, and a never-fetched failure shows the honest error screen.
 */
import { useCallback, useMemo, useState } from 'react';
import {
  ActivityIndicator,
  Pressable,
  RefreshControl,
  SectionList,
  StyleSheet,
  Text,
  TextInput,
  View,
} from 'react-native';
import { StatusBar } from 'expo-status-bar';
import { useNavigation } from '@react-navigation/native';
import type { NativeStackNavigationProp } from '@react-navigation/native-stack';

import {
  buildBrowseSections,
  buildSearchSections,
  cheapestPrice,
  CHUNK_SIZE,
  formatDate,
  packMeta,
  type FeedPack,
} from './feed';
import type { RootStackParamList } from './navigation';
import { useFeed } from './useFeed';

export default function CatalogScreen() {
  const navigation = useNavigation<NativeStackNavigationProp<RootStackParamList>>();
  const { state: status, refresh } = useFeed();
  const [query, setQuery] = useState('');
  const [visibleCount, setVisibleCount] = useState(CHUNK_SIZE);

  const searching = query.trim().length > 0;
  const packs = useMemo(
    () => (status.kind === 'ready' ? status.feed.packs : []),
    [status]
  );

  const { sections, hasMore } = useMemo(() => {
    if (status.kind !== 'ready') return { sections: [], hasMore: false };
    return searching
      ? buildSearchSections(packs, query, visibleCount)
      : buildBrowseSections(packs, visibleCount);
  }, [status.kind, packs, searching, query, visibleCount]);

  const openPack = useCallback(
    (pack: FeedPack) => {
      navigation.navigate('Comparison', {
        catalog_id: pack.catalog_id,
        name: pack.name,
        brand: pack.brand,
      });
    },
    [navigation]
  );

  const renderCard = useCallback(
    (pack: FeedPack) => {
      const price = cheapestPrice(pack);
      return (
        <Pressable key={pack.catalog_id} style={styles.card} onPress={() => openPack(pack)}>
          <Text style={styles.cardName}>{pack.name}</Text>
          <Text style={styles.cardMeta}>{packMeta(pack)}</Text>
          {price != null ? (
            <>
              <Text style={styles.cardPrice}>€{price}</Text>
              <Text style={styles.cardPriceNote}>cheapest now</Text>
            </>
          ) : (
            <>
              <Text style={[styles.cardPrice, styles.cardPriceEmpty]}>—</Text>
              <Text style={styles.cardPriceNote}>no prices yet</Text>
            </>
          )}
        </Pressable>
      );
    },
    [openPack]
  );

  const renderRow = useCallback(
    ({ item }: { item: [FeedPack, FeedPack | null] }) => (
      <View style={styles.row}>
        {renderCard(item[0])}
        {item[1] ? renderCard(item[1]) : <View style={styles.cardPlaceholder} />}
      </View>
    ),
    [renderCard]
  );

  return (
    <View style={styles.screen}>
      <StatusBar style="light" />

      {status.kind === 'loading' && (
        <View style={[styles.centered, styles.screen]}>
          <ActivityIndicator size="large" color="#0B3D2E" />
          <Text style={styles.muted}>Loading feed…</Text>
        </View>
      )}

      {status.kind === 'error' && (
        <View style={[styles.centered, styles.screen]}>
          <Text style={styles.errorTitle}>Couldn&apos;t load the feed</Text>
          <Text style={styles.muted}>{status.message}</Text>
          <Pressable style={styles.retryButton} onPress={refresh}>
            <Text style={styles.retryLabel}>Retry</Text>
          </Pressable>
        </View>
      )}

      {status.kind === 'ready' && (
        <>
          {/* Deep green hero — "Find the cheaper bottle." */}
          <View style={styles.hero}>
            <Text style={styles.heroTitle}>
              Find the <Text style={styles.heroAccent}>cheaper</Text> bottle.
            </Text>
            <Text style={styles.heroSub}>
              Every shelf price across Ireland&apos;s five big grocers — one exact pack
              at a time.
            </Text>
            {/* Spec §5: the payload on screen is always the last successful
                fetch, so it is always labelled with its fetch date. */}
            <Text style={styles.heroAsOf}>as of {formatDate(status.asOf)}</Text>
          </View>

          {/* Search bar first — overlaps the hero's rounded bottom edge. */}
          <TextInput
            style={styles.search}
            placeholder={`Search ${packs.length} packs…`}
            placeholderTextColor="#6D7F72"
            value={query}
            onChangeText={(text) => {
              setQuery(text);
              setVisibleCount(CHUNK_SIZE);
            }}
            autoCorrect={false}
            autoCapitalize="none"
          />

          {/* Spec §5: failed refresh with cache present — non-blocking;
              the cached data below stays untouched. */}
          {status.banner != null && (
            <View style={styles.staleBanner}>
              <Text style={styles.staleBannerText}>{status.banner}</Text>
            </View>
          )}

          <SectionList
            sections={sections}
            keyExtractor={(item) => item[0].catalog_id}
            renderItem={renderRow}
            renderSectionHeader={({ section }) => (
              <Text style={styles.sectionHeader}>
                {section.title}
                <Text style={styles.sectionCount}> · {section.count}</Text>
              </Text>
            )}
            stickySectionHeadersEnabled
            contentContainerStyle={styles.listContent}
            onEndReached={hasMore ? () => setVisibleCount((n) => n + CHUNK_SIZE) : undefined}
            onEndReachedThreshold={0.5}
            ListEmptyComponent={
              <Text style={styles.empty}>
                {searching
                  ? 'Nothing matches. Try another brand.'
                  : 'No packs in the feed yet.'}
              </Text>
            }
            ListFooterComponent={
              hasMore ? <Text style={styles.loadingMore}>Loading more packs…</Text> : null
            }
            refreshControl={
              <RefreshControl
                refreshing={status.refreshing}
                onRefresh={refresh}
                tintColor="#0B3D2E"
                colors={['#0B3D2E']}
              />
            }
          />
        </>
      )}
    </View>
  );
}

const styles = StyleSheet.create({
  screen: {
    flex: 1,
    backgroundColor: '#F2EFE7',
  },
  hero: {
    backgroundColor: '#0B3D2E',
    paddingTop: 60,
    paddingHorizontal: 18,
    paddingBottom: 32,
    borderBottomLeftRadius: 26,
    borderBottomRightRadius: 26,
  },
  heroTitle: {
    color: '#EEF4EE',
    fontSize: 25,
    fontWeight: '800',
    letterSpacing: -0.4,
    lineHeight: 30,
  },
  heroAccent: {
    color: '#FFD166',
  },
  heroSub: {
    color: '#9DBFAD',
    fontSize: 12,
    marginTop: 5,
  },
  heroAsOf: {
    color: '#FFD166',
    fontSize: 11,
    fontWeight: '700',
    marginTop: 8,
  },
  staleBanner: {
    backgroundColor: '#FFD166',
    borderRadius: 14,
    marginHorizontal: 16,
    marginTop: 10,
    paddingVertical: 9,
    paddingHorizontal: 14,
  },
  staleBannerText: {
    color: '#0B3D2E',
    fontSize: 12,
    fontWeight: '700',
  },
  search: {
    backgroundColor: '#FFFFFF',
    borderRadius: 999,
    paddingHorizontal: 16,
    paddingVertical: 12,
    fontSize: 14,
    color: '#0B3D2E',
    marginHorizontal: 16,
    marginTop: -16,
    shadowColor: '#0B3D2E',
    shadowOpacity: 0.25,
    shadowRadius: 7,
    shadowOffset: { width: 0, height: 4 },
    elevation: 5,
  },
  listContent: {
    paddingHorizontal: 16,
    paddingBottom: 24,
  },
  sectionHeader: {
    backgroundColor: '#F2EFE7',
    paddingTop: 14,
    paddingBottom: 8,
    fontSize: 12,
    fontWeight: '800',
    letterSpacing: 1.1,
    textTransform: 'uppercase',
    color: '#0B3D2E',
  },
  sectionCount: {
    color: '#6D7F72',
    fontWeight: '600',
    letterSpacing: 0,
  },
  row: {
    flexDirection: 'row',
    gap: 10,
    marginBottom: 10,
  },
  card: {
    flex: 1,
    backgroundColor: '#FFFFFF',
    borderRadius: 18,
    padding: 13,
    shadowColor: '#0B3D2E',
    shadowOpacity: 0.12,
    shadowRadius: 2,
    shadowOffset: { width: 0, height: 1 },
    elevation: 2,
    gap: 4,
  },
  cardPlaceholder: {
    flex: 1,
  },
  cardName: {
    fontSize: 13,
    fontWeight: '700',
    lineHeight: 17,
    color: '#0B3D2E',
  },
  cardMeta: {
    fontSize: 10.5,
    color: '#6D7F72',
  },
  cardPrice: {
    marginTop: 'auto',
    paddingTop: 6,
    fontSize: 17,
    fontWeight: '800',
    color: '#0B3D2E',
  },
  cardPriceEmpty: {
    color: '#6D7F72',
    fontWeight: '600',
  },
  cardPriceNote: {
    fontSize: 10,
    fontWeight: '600',
    color: '#6D7F72',
  },
  empty: {
    textAlign: 'center',
    color: '#3F5449',
    fontSize: 13,
    paddingVertical: 32,
  },
  loadingMore: {
    textAlign: 'center',
    color: '#6D7F72',
    fontSize: 12,
    paddingVertical: 16,
  },
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
