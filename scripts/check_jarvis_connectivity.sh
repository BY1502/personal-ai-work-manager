#!/usr/bin/env bash
set -euo pipefail

API_BASE="${API_BASE_URL:-http://127.0.0.1:8100}"
DASHBOARD_1="${DASHBOARD_URL_1:-http://127.0.0.1:3100}"
DASHBOARD_2="${DASHBOARD_URL_2:-http://127.0.0.1:3001}"
DASHBOARD_0="${DASHBOARD_URL_0:-http://127.0.0.1:3000}"
if [ -z "${BACKEND_BASE_URL:-}" ]; then
  if echo "$API_BASE" | grep -qE ":(8100|8000|443|80)"; then
    BACKEND_BASE="$API_BASE"
  else
    BACKEND_BASE="http://127.0.0.1:8100"
  fi
else
  BACKEND_BASE="${BACKEND_BASE_URL}"
fi
EXPECT_SNIPPET="BY"
CHECK_LAN="${BY_CHECK_LAN:-0}"
HOST_IP="$(ifconfig | awk '/inet / && !/127\.0\.0\.1/ {print $2; exit}')"
AUTH_USERNAME="${BY_USERNAME:-}"
AUTH_PASSWORD="${BY_PASSWORD:-}"
COOKIE_JAR=""
AUTH_BODY=""
AUTHENTICATED=0

cleanup() {
  if [ "$AUTHENTICATED" = "1" ] && [ -n "$COOKIE_JAR" ]; then
    curl -sS --max-time 5 -b "$COOKIE_JAR" \
      -X POST "$API_BASE/api/v1/auth/logout" >/dev/null 2>&1 || true
  fi
  if [ -n "$COOKIE_JAR" ]; then
    rm -f "$COOKIE_JAR"
  fi
  if [ -n "$AUTH_BODY" ]; then
    rm -f "$AUTH_BODY"
  fi
}
trap cleanup EXIT

login_if_configured() {
  if [ -z "$AUTH_USERNAME" ] && [ -z "$AUTH_PASSWORD" ]; then
    return 1
  fi
  if [ -z "$AUTH_USERNAME" ] || [ -z "$AUTH_PASSWORD" ]; then
    echo "BY_USERNAME and BY_PASSWORD must be set together" >&2
    exit 1
  fi
  if ! command -v python3 >/dev/null 2>&1; then
    echo "python3 is required for the authenticated connectivity check" >&2
    exit 1
  fi

  COOKIE_JAR="$(mktemp)"
  AUTH_BODY="$(mktemp)"
  chmod 600 "$COOKIE_JAR" "$AUTH_BODY"
  AUTH_USERNAME="$AUTH_USERNAME" AUTH_PASSWORD="$AUTH_PASSWORD" \
    python3 - "$AUTH_BODY" <<'PY'
import json
import os
import sys

with open(sys.argv[1], "w", encoding="utf-8") as destination:
    json.dump(
        {"username": os.environ["AUTH_USERNAME"], "password": os.environ["AUTH_PASSWORD"]},
        destination,
    )
PY
  curl -fsS --max-time 10 \
    -H 'Content-Type: application/json' \
    -c "$COOKIE_JAR" -b "$COOKIE_JAR" \
    --data-binary "@$AUTH_BODY" \
    "$API_BASE/api/v1/auth/login" >/dev/null
  AUTHENTICATED=1
  rm -f "$AUTH_BODY"
  AUTH_BODY=""
}

json_get() {
  local url="$1"
  if [ -n "$COOKIE_JAR" ]; then
    curl -fsS --max-time 5 -b "$COOKIE_JAR" "$url"
  else
    curl -fsS --max-time 5 "$url"
  fi
}

dashboard_html_contains_by() {
  local url="$1"
  local body
  if ! body="$(curl -fsS --max-time 5 "$url" | tr -d '\r' | tr '\n' ' ')"; then
    return 1
  fi
  if printf '%s' "$body" | grep -q "$EXPECT_SNIPPET"; then
    return 0
  fi
  return 1
}

echo "[1/5] API health"
json_get "$BACKEND_BASE/health" >/dev/null

if login_if_configured; then
  echo "[2/5] Authenticated API summary"
  json_get "$API_BASE/api/v1/dashboard/summary"

  echo "[3/5] Authenticated API provider status"
  json_get "$API_BASE/api/v1/dashboard/provider"
else
  echo "[2/5] Authenticated API summary (skipped: set BY_USERNAME/BY_PASSWORD)"
  echo "[3/5] Authenticated API provider status (skipped)"
fi

echo "[4/5] Dashboard HTML ($DASHBOARD_1)"
curl -fsS --max-time 5 "$DASHBOARD_1" >/dev/null && echo "ok"

echo "[5/5] Dashboard HTML ($DASHBOARD_2)"
curl -fsS --max-time 5 "$DASHBOARD_2" >/dev/null && echo "ok"

if [[ "$CHECK_LAN" = "1" && -n "$HOST_IP" ]]; then
  echo "[extra] Explicit LAN dashboard checks ($HOST_IP)"
  curl -fsS --max-time 5 "http://${HOST_IP}:3100" >/dev/null && echo "3100 ok"
  curl -fsS --max-time 5 "http://${HOST_IP}:3001" >/dev/null && echo "3001 ok"
  echo "[extra] CORS preflight for explicitly configured LAN origin ($HOST_IP)"
  for u in "http://${HOST_IP}:3100" "http://${HOST_IP}:3001"; do
    if curl -fsS -o /dev/null -w '%{http_code}' \
      -X OPTIONS \
      -H "Origin: ${u}" \
      -H "Access-Control-Request-Method: POST" \
      -H "Access-Control-Request-Headers: content-type" \
      "$API_BASE/api/v1/chat/runs"; then
      echo "${u} -> preflight ok"
    else
      echo "${u} -> preflight fail"
    fi
  done
fi

echo "[extra] Checking legacy port ($DASHBOARD_0)"
if dashboard_html_contains_by "$DASHBOARD_0"; then
  echo "warning: 3000 is responding from BY"
else
  echo "info: 3000 is not BY. If you opened 3000 and saw an error, that's expected."
fi

echo "connectivity check passed"
