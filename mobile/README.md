# Drinks Tracker — mobile app

React Native + Expo (TypeScript), one codebase for Android + iOS. Consumes the
read-only Price Feed (`GET /consumer/feed`); never collects or mutates anything.
Spec: `../.scratch/mobile-app/spec.md`.

## Run (dev, LAN API)

```sh
cp .env.example .env        # points EXPO_PUBLIC_API_BASE_URL at the LAN API
npm install
npm start                   # then scan the QR with Expo Go (dev iteration)
```

Plain-HTTP LAN URLs work in Expo Go. A dedicated Android *dev build* (via EAS)
needs `expo-build-properties` with `android.usesCleartextTraffic: true` — add it
when moving off Expo Go.

## Checks

```sh
npm run typecheck
npm run lint
npx expo export --platform android   # Metro bundle smoke test
```

## EAS builds (no local Mac)

`eas build --profile <development|preview|production>` runs in EAS cloud for
both platforms; iOS never needs local Xcode. Each profile bakes in its
`EXPO_PUBLIC_API_BASE_URL` (see `eas.json`). Requires the operator's Expo
account (`eas login`); the production profile's URL is a placeholder until the
Cloudflare Tunnel deployment (tickets 10/15) lands the real domain.
