/**
 * Pack comparison route — PLACEHOLDER (ticket 12 wiring, spec §3.2).
 *
 * Ticket 13 fills this in with the full Exact-Pack Comparison: cheapest
 * observed retailer as a hero card, remaining retailers as smaller rows,
 * the five §4 states as subdued rows, DRS always its own line, Clubcard
 * pill omitted while null. Until then this stub only proves the route
 * exists and receives the tapped pack.
 */
import { Pressable, StyleSheet, Text, View } from 'react-native';
import { StatusBar } from 'expo-status-bar';
import { useNavigation, useRoute } from '@react-navigation/native';
import type { NativeStackNavigationProp } from '@react-navigation/native-stack';

import type { RootStackParamList } from './navigation';

export default function ComparisonScreen() {
  const navigation = useNavigation<NativeStackNavigationProp<RootStackParamList>>();
  const route = useRoute<{ key: string; name: 'Comparison'; params?: RootStackParamList['Comparison'] }>();
  const pack = route.params;

  return (
    <View style={styles.screen}>
      <StatusBar style="dark" />
      <Pressable style={styles.back} onPress={() => navigation.goBack()}>
        <Text style={styles.backLabel}>‹ All packs</Text>
      </Pressable>
      <Text style={styles.title}>{pack ? pack.name : 'Pack comparison'}</Text>
      <Text style={styles.body}>
        Exact-Pack Comparison lands here in ticket 13 (spec §3.2).
      </Text>
    </View>
  );
}

const styles = StyleSheet.create({
  screen: {
    flex: 1,
    backgroundColor: '#F2EFE7',
    paddingHorizontal: 16,
    paddingTop: 60,
  },
  back: {
    alignSelf: 'flex-start',
    paddingVertical: 8,
    paddingRight: 16,
  },
  backLabel: {
    color: '#0B3D2E',
    fontSize: 14,
    fontWeight: '600',
  },
  title: {
    color: '#0B3D2E',
    fontSize: 20,
    fontWeight: '800',
    letterSpacing: -0.2,
    marginTop: 8,
  },
  body: {
    color: '#6D7F72',
    fontSize: 13,
    marginTop: 10,
  },
});
