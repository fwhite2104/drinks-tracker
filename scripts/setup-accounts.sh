#!/usr/bin/env bash
# /wizard-style interactive setup for the human-only steps in
# .scratch/mobile-app/issues/15-human-setup-accounts-and-domain.md
# (spec §7 items 3–4, §2 distribution decisions).
#
# Walks the operator through four account/domain steps, validating each value
# as it is captured, and writes the results atomically to .env.local-accounts
# (mode 600, git-ignored — NEVER commit real values).
#
# Idempotent: values already present in .env.local-accounts are shown and the
# step is skipped. Delete the file to re-run everything.
#
# Consumers of the output:
#   - deploy/README.md + ticket 10   → CLOUDFLARE_DOMAIN (api.<domain> tunnel)
#   - mobile/app.json                → ANDROID_PACKAGE (android.package)
#   - mobile/eas.json + .env.example → production base URL (api.<domain>)
#   - tickets 11–14 production crit. → APPLE_TEAM_ID, ANDROID_PACKAGE,
#                                      EXPO_USERNAME (real EAS build)
set -uo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
OUT_FILE="$REPO_ROOT/.env.local-accounts"

# ---------------------------------------------------------------- helpers ---

die() { printf 'error: %s\n' "$1" >&2; exit 1; }

# Print URL and offer to open it in a browser. EOF (non-interactive stdin)
# defaults to "no" instead of aborting.
open_url() {
  local url="$1" ans=""
  printf '\n  URL: %s\n' "$url"
  read -r -p "  Open this URL in a browser? [y/N] " ans || ans="n"
  if [[ "$ans" =~ ^[Yy] ]] && command -v xdg-open >/dev/null 2>&1; then
    xdg-open "$url" >/dev/null 2>&1 &
    disown || true
  fi
}

# prompt_value <var-name> <prompt> <validator-fn>
# Reads until the validator accepts. EOF on stdin is fatal (non-interactive).
prompt_value() {
  local __var="$1" __prompt="$2" __validate="$3" input
  while true; do
    printf '\n%s' "$__prompt"
    if ! IFS= read -r input; then
      die "input ended before value for ${__var} was captured — nothing written"
    fi
    if "$__validate" "$input"; then
      printf -v "$__var" '%s' "$input"
      return 0
    fi
    printf '  ✗ invalid value, try again (empty input to abort: Ctrl-D)\n' >&2
  done
}

# ------------------------------------------------------------- validators ---

# Domain: lowercase, no scheme, no trailing dot, ≥2 labels, valid hostnames.
validate_domain() {
  local d="${1,,}"          # lowercase
  [[ "$d" == "$1" ]] || { printf '    (use lowercase — no uppercase in domains)\n' >&2; return 1; }
  [[ -n "$d" && "$d" != *"." && "$d" != *"://"* && "$d" != *" "* && "$d" != *"。" && "$d" == *"."* ]] \
    && [[ "$d" =~ ^([a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?\.)+[a-z]{2,63}$ ]]
}

# Apple Team ID: exactly 10 alphanumeric characters (uppercased on capture).
validate_team_id() {
  [[ "$1" =~ ^[A-Za-z0-9]{10}$ ]]
}

# Android package: reverse-DNS, lowercase, ≥2 segments, each segment starts
# with a letter (Android requirement) — e.g. com.example.drinks or
# ie.yourname.drinkstracker.
validate_package() {
  [[ "$1" =~ ^[a-z][a-z0-9_]*(\.[a-z][a-z0-9_]*)+$ ]]
}

# Expo username: 2–64 chars, letters/digits/hyphen/underscore.
validate_username() {
  [[ "$1" =~ ^[A-Za-z0-9_-]{2,64}$ ]]
}

# ----------------------------------------------------------------- state ---

# Read a KEY=value pair out of the existing output file (no sourcing).
get_saved() {
  local key="$1"
  [[ -f "$OUT_FILE" ]] || return 1
  grep -E "^${key}=" "$OUT_FILE" 2>/dev/null | tail -n 1 | cut -d= -f2-
}

DOMAIN="" APPLE_TEAM_ID="" ANDROID_PACKAGE="" EXPO_USERNAME=""

# Load saved values; invalid/corrupt saved values are treated as absent so the
# step re-runs.
load_saved() {
  local v
  v="$(get_saved CLOUDFLARE_DOMAIN)" && validate_domain "$v" && DOMAIN="$v"
  v="$(get_saved APPLE_TEAM_ID)"     && validate_team_id "$v" && APPLE_TEAM_ID="$v"
  v="$(get_saved ANDROID_PACKAGE)"   && validate_package "$v" && ANDROID_PACKAGE="$v"
  v="$(get_saved EXPO_USERNAME)"     && validate_username "$v" && EXPO_USERNAME="$v"
  return 0
}

# Ask-and-skip: if var already holds a valid value, report and skip.
skip_if_set() { # <var-name> <label>
  local __val="${!1}"
  if [[ -n "$__val" ]]; then
    printf '\n[skip] %s — already captured: %s\n' "$2" "$__val"
    return 0
  fi
  return 1
}

# ------------------------------------------------------------ the wizard ---

step_domain() {
  skip_if_set DOMAIN "Step 1 · Domain" && return 0
  cat <<'EOF'

━━━ Step 1 / 4 · Domain purchase (Cloudflare Registrar, ~€10/yr) ━━━━━━━━━━━━
Buy a domain and keep it on Cloudflare DNS (required for ticket 10's tunnel:
the cloudflared DNS route creates api.<domain> directly, no third-party DNS).
Picking something short and cheap is fine — e.g. <yourname>.ie/.dev/.app.
EOF
  open_url "https://dash.cloudflare.com/?to=/:account/domains/register"
  prompt_value DOMAIN \
    "  Enter the domain you purchased (e.g. example.dev): " validate_domain
  printf '\n  ✓ Next step for ticket 10: its runbook (deploy/README.md §3) will\n'
  printf '    route the DNS record  api.%s  through your tunnel — keep that\n' "$DOMAIN"
  printf '    hostname handy when you fill in deploy/config.yml.\n'
}

step_apple() {
  skip_if_set APPLE_TEAM_ID "Step 2 · Apple Developer Team ID" && return 0
  cat <<'EOF'

━━━ Step 2 / 4 · Apple Developer Program ($99/yr) ━━━━━━━━━━━━━━━━━━━━━━━━━━
⚠ Enrollment involves a multi-day review/approval lead time — start it NOW:
  it sits on the critical path for every iOS build (tickets 11–14 and 16
  all need a signing team before a TestFlight build can exist).
Once enrolled, find your Team ID: developer.apple.com/account → Membership
details (a 10-character alphanumeric code).
EOF
  open_url "https://developer.apple.com/programs/enroll/"
  prompt_value APPLE_TEAM_ID \
    "  Enter your Apple Developer Team ID (10 chars, once active): " validate_team_id
  printf -v APPLE_TEAM_ID '%s' "${APPLE_TEAM_ID^^}"   # normalise to uppercase
}

step_google() {
  skip_if_set ANDROID_PACKAGE "Step 3 · Google Play / Android package" && return 0
  cat <<'EOF'

━━━ Step 3 / 4 · Google Play Console ($25 one-time) ━━━━━━━━━━━━━━━━━━━━━━━━
Register the developer account, then reserve your app's Android package
name. Convention: reverse-DNS of something you own, all lowercase, e.g.
  ie.<yourname>.drinkstracker
It must match mobile/app.json's "android.package" exactly when that is set
(ticket 16 wires it) — package names are permanent once an app is uploaded.
EOF
  open_url "https://play.google.com/console/signup"
  prompt_value ANDROID_PACKAGE \
    "  Enter the reserved Android package name (reverse-DNS, lowercase): " validate_package
}

step_expo() {
  skip_if_set EXPO_USERNAME "Step 4 · Expo account" && return 0
  cat <<'EOF'

━━━ Step 4 / 4 · Expo account (free) ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
Create a free Expo account. Afterwards, inside mobile/ run:
  eas login            # with this account
  eas build:init       # one-time EAS project setup
Tickets 11–14's production criteria require at least one real EAS build
(Android + iOS) from this account.
EOF
  open_url "https://expo.dev/signup"
  prompt_value EXPO_USERNAME \
    "  Enter your Expo account username: " validate_username
}

# ----------------------------------------------------------------- write ---

ensure_ignored() {
  # The output file must never be committable. .gitignore in this repo already
  # covers .env.* — verify with git; if uncovered, append a literal rule.
  if git -C "$REPO_ROOT" rev-parse --is-inside-work-tree >/dev/null 2>&1; then
    git -C "$REPO_ROOT" check-ignore -q -- ".env.local-accounts" && return 0
    printf '\n# Local account-setup values (ticket 15) — never commit\n.env.local-accounts\n' \
      >> "$REPO_ROOT/.gitignore"
  else
    grep -qE '^(\.env\.local-accounts|\.env\.\*)$' "$REPO_ROOT/.gitignore" 2>/dev/null && return 0
    printf '\n# Local account-setup values (ticket 15) — never commit\n.env.local-accounts\n' \
      >> "$REPO_ROOT/.gitignore"
  fi
}

write_results() {
  ensure_ignored
  # Atomic write: temp file in the same directory, 600 perms before the rename.
  local tmp
  tmp="$(mktemp "$REPO_ROOT/.env.local-accounts.XXXXXX")" || die "cannot create temp file"
  chmod 600 "$tmp" || { rm -f "$tmp"; die "chmod failed"; }
  cat >"$tmp" <<EOF
# Drinks Tracker — human account setup values (ticket 15)
# NEVER commit this file. Consumers:
#   deploy/README.md + ticket 10   → CLOUDFLARE_DOMAIN (api.<domain> tunnel)
#   mobile/app.json                → ANDROID_PACKAGE (android.package)
#   mobile/.env.example + eas.json → production base URL https://api.<domain>
#   tickets 11–14 production       → APPLE_TEAM_ID / ANDROID_PACKAGE / EXPO_USERNAME
CLOUDFLARE_DOMAIN=$DOMAIN
APPLE_TEAM_ID=$APPLE_TEAM_ID
ANDROID_PACKAGE=$ANDROID_PACKAGE
EXPO_USERNAME=$EXPO_USERNAME
EOF
  mv -f "$tmp" "$OUT_FILE" || { rm -f "$tmp"; die "atomic rename failed"; }
  chmod 600 "$OUT_FILE"
}

summary() {
  cat <<EOF

━━━ Summary ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
Written to $OUT_FILE (mode 600, git-ignored — NEVER commit):
  CLOUDFLARE_DOMAIN  = $DOMAIN
  APPLE_TEAM_ID      = $APPLE_TEAM_ID
  ANDROID_PACKAGE    = $ANDROID_PACKAGE
  EXPO_USERNAME      = $EXPO_USERNAME

Unblocks, in order:
  • Ticket 15 → 16: all four values collected; ticket 16 can wire
    android.package into mobile/app.json and the real values into eas.json.
  • Ticket 10 (Cloudflare deployment): deploy/README.md §3 + config.yml use
    api.$DOMAIN — the tunnel DNS route and Access apps need it.
  • Tickets 11–14 (production criteria): eas login as $EXPO_USERNAME,
    eas build:init, then one real EAS build with APPLE_TEAM_ID signing and
    ANDROID_PACKAGE on the Android side.
EOF
}

main() {
  cat <<'EOF'
Drinks Tracker — account & domain setup wizard (ticket 15)
==========================================================
Four human-only steps: domain, Apple, Google Play, Expo.
Each step shows what to do and offers to open the right URL.
EOF
  load_saved

  local missing=0
  [[ -z "$DOMAIN" || -z "$APPLE_TEAM_ID" || -z "$ANDROID_PACKAGE" || -z "$EXPO_USERNAME" ]] && missing=1

  if (( missing )) && ! [[ -t 0 ]]; then
    die "stdin is not a terminal and some steps are incomplete — run me interactively; nothing was written"
  fi
  if (( ! missing )) && [[ -f "$OUT_FILE" ]]; then
    printf '\nAll steps already complete (%s). Delete it to re-run.\n' "$OUT_FILE"
    return 0
  fi

  step_domain
  step_apple
  step_google
  step_expo
  write_results
  summary
}

if [[ "${BASH_SOURCE[0]}" == "$0" ]]; then
  main "$@"
fi
