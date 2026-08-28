#!/usr/bin/env sh
# Post-deployment health check for the public API (spec §7.2, ticket 10).
#
# Usage:   ./deploy/healthcheck.sh [BASE_URL]
#          BASE_URL defaults to http://localhost:8000; pass the public URL
#          from outside the LAN to verify the tunnel path end-to-end:
#              ./deploy/healthcheck.sh https://api.<your-domain>
#          or: make deploy-check BASE_URL=https://api.<your-domain>
#
# Checks that /health reports status "ok" and /consumer/feed returns valid
# JSON. Exits non-zero on the first failure.
set -eu

BASE_URL="${1:-http://localhost:8000}"
fail() {
    echo "FAIL: $1" >&2
    exit 1
}

echo "Checking ${BASE_URL} ..."

# /health: 200 + status ok
HEALTH=$(curl -fsS --max-time 15 "${BASE_URL}/health") \
    || fail "GET /health failed (non-2xx, unreachable, or invalid response)"
echo "$HEALTH" | grep -q '"status"[[:space:]]*:[[:space:]]*"ok"' \
    || fail "/health did not report status ok: ${HEALTH}"
echo "ok: /health status=ok"

# /consumer/feed: 200 + JSON object
FEED=$(curl -fsS --max-time 15 "${BASE_URL}/consumer/feed") \
    || fail "GET /consumer/feed failed (non-2xx, unreachable, or invalid response)"
echo "$FEED" | grep -q '^{.*}$' \
    || fail "/consumer/feed did not return a JSON object"
echo "ok: /consumer/feed JSON object ($(printf '%s' "$FEED" | wc -c) bytes)"

echo "PASS: ${BASE_URL} is serving the public consumer API."
