# Mobile App

The consumer client: anonymous Android + iOS app for Irish shoppers. Pick a
soft drink, see which Tier-1 retailer is cheapest right now via Exact-Pack
Comparison. Locked decisions live in `.scratch/mobile-app/spec.md`
(gitignored tracker); product intent in [`../goal.md`](../goal.md).

## Stack & build

- **React Native + Expo (TypeScript)**, one codebase for Android + iOS.
- **iOS builds via EAS cloud** — no local Mac. `eas.json` carries
  development / preview / production profiles.
- **API base URL** is the build-time env var `EXPO_PUBLIC_API_BASE_URL`
  (`mobile/.env.example` documents both): dev defaults to the LAN API
  (`http://<lan-ip>:8000`), production is `https://api.<your-domain>` until
  the domain lands. Dedicated Android dev builds allow cleartext LAN traffic
  via `expo-build-properties` (`android.usesCleartextTraffic`) — Expo Go
  cannot.
- `npm run typecheck` (strict tsc) and `npm run lint` must stay clean;
  `node_modules` / `.expo` / native folders are never committed.

## The contract (server-owned state machine)

The app consumes `GET /consumer/feed` and **renders only**. Per
pack×retailer the server sends one of five states with its own `label`:

| State | Meaning |
|---|---|
| `observed` | current observation — Displayed Price shown; Clubcard price beside (never instead), DRS deposit its own line, component unit price for multipacks |
| `last_seen` | past observation — "Last seen \<date\>" is a fact, not a reused price |
| `awaiting_price` | mapping approved, observation not yet collected |
| `temporarily_unavailable` | known listing, temporarily unpurchasable |
| `not_available` | explicit retailer exclusion on record |

Rules the app must never break:

- **Never re-derive state or prices client-side** — the server's `label` is
  the only label shown.
- **Never lie about absence**: no stock claims, no retirement claims, no
  synthetic or demo prices in any state.
- Cheapest-first ordering with highlight; observed dates + a "may be out of
  date" note after 7 days; DRS deposit always a separate line.

## Screens

- **Catalog (first open)** — search-first: one search bar over a scrollable
  catalog grouped into brand sections (lazy-loaded chunks); tap a pack →
  comparison. Truthful empty/slow states; no curated subset, no grid screen.
- **Comparison** — cheapest retailer is the hero (big card, big price);
  every other retailer is a smaller row; per-retailer state rendering per
  the five-state contract; no history charts (Last Seen only).

## Offline & degraded network

- Last-good cache renders immediately, labelled **"as of \<date\>"**.
- Background refresh; a failed refresh shows a non-blocking banner.
- Honest error screen **only** when nothing has ever been fetched.
- The cache holds **raw API responses only** (newest fetch wins, never
  merged) — there is no code path, offline or corrupt, where a non-API price
  can appear. Outside `__DEV__` a state-preview fixture returns `null` so
  release builds can't show demo data.

## Verification state & pending human steps

Typecheck, lint, bundle, live-API, and dead-port checks all pass; the app
proves endpoint→app connectivity against the real compose `api`. Resolution
of the remaining tickets rides on named **human/operator steps** (tracked in
the gitignored tracker, `.scratch/mobile-app/issues/`):

- **10 — public deployment**: domain on Cloudflare DNS + tunnel + Access per
  [`../deploy/README.md`](../deploy/README.md), then `make deploy-check`.
- **11 — EAS builds**: `eas login` + a green EAS cloud build on Android and
  iOS (needs the operator's Expo account); production base URL wiring once
  10/15 land.
- **12–14 — on-device pass**: Expo Go over LAN, cache survival, banner
  rendering on real hardware.
- **15 — accounts & domain**: domain purchase + Expo account (immediate),
  Apple Developer / Google Play (deferred until code phase completes) —
  `scripts/setup-accounts.sh` walks the operator through it; store
  distribution (TestFlight / Play internal testing) is the final gate.
