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
EXPECT_SNIPPET="JARVIS"
HOST_IP="$(ifconfig | awk '/inet / && !/127\.0\.0\.1/ {print $2; exit}')"

json_get() {
  local url="$1"
  curl -fsS --max-time 5 "$url"
}

dashboard_html_contains_jarvis() {
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
if ! json_get "$BACKEND_BASE/health" >/dev/null; then
  json_get "$API_BASE/api/v1/dashboard/provider" >/dev/null
fi

echo "[2/5] API summary"
json_get "$API_BASE/api/v1/dashboard/summary"

echo "[3/5] API provider status"
json_get "$API_BASE/api/v1/dashboard/provider"

echo "[4/5] Dashboard HTML ($DASHBOARD_1)"
curl -fsS --max-time 5 "$DASHBOARD_1" >/dev/null && echo "ok"

echo "[5/5] Dashboard HTML ($DASHBOARD_2)"
curl -fsS --max-time 5 "$DASHBOARD_2" >/dev/null && echo "ok"

if [[ -n "$HOST_IP" ]]; then
  echo "[extra] Local IP dashboard checks ($HOST_IP)"
  curl -fsS --max-time 5 "http://${HOST_IP}:3100" >/dev/null && echo "3100 ok"
  curl -fsS --max-time 5 "http://${HOST_IP}:3001" >/dev/null && echo "3001 ok"
  echo "[extra] CORS preflight from local IP ($HOST_IP)"
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
if dashboard_html_contains_jarvis "$DASHBOARD_0"; then
  echo "warning: 3000 is responding from JARVIS"
else
  echo "info: 3000 is not JARVIS. If you opened 3000 and saw an error, that's expected."
fi

echo "connectivity check passed"
