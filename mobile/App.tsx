/**
 * Drinks Tracker — anonymous consumer app (spec: .scratch/mobile-app/spec.md).
 *
 * Navigation: search-first catalog (ticket 12) → exact-pack comparison
 * (ticket 13, placeholder route for now). Feed access: GET /consumer/feed
 * from the build-time EXPO_PUBLIC_API_BASE_URL (spec §8).
 */
import { NavigationContainer } from '@react-navigation/native';
import { createNativeStackNavigator } from '@react-navigation/native-stack';

import CatalogScreen from './src/CatalogScreen';
import ComparisonScreen from './src/ComparisonScreen';
import type { RootStackParamList } from './src/navigation';

const Stack = createNativeStackNavigator<RootStackParamList>();

export default function App() {
  return (
    <NavigationContainer>
      <Stack.Navigator screenOptions={{ headerShown: false }}>
        <Stack.Screen name="Catalog" component={CatalogScreen} />
        <Stack.Screen name="Comparison" component={ComparisonScreen} />
      </Stack.Navigator>
    </NavigationContainer>
  );
}
