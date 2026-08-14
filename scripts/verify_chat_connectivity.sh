#!/usr/bin/env bash
set -euo pipefail

API_BASE="${API_BASE_URL:-http://127.0.0.1:8100}"
BACKEND_BASE="${BACKEND_BASE_URL:-}"
ITERATIONS="${1:-20}"
TIMEOUT="${2:-180}"
STRESS_TEST="${3:-0}"

DASHBOARD_PORT_HINT="${DASHBOARD_PORT:-3100}"
if [ -z "$BACKEND_BASE" ]; then
  if echo "$API_BASE" | grep -qE ":(8100|80|8000|443)"; then
    BACKEND_BASE="$API_BASE"
  else
    BACKEND_BASE="http://127.0.0.1:8100"
  fi
fi

if ! command -v jq >/dev/null 2>&1; then
  echo "jq가 설치되어 있지 않습니다. brew install jq" >&2
  exit 1
fi

if ! command -v python3 >/dev/null 2>&1; then
  echo "python3가 설치되어 있지 않습니다." >&2
  exit 1
fi

echo "[1/5] API health"
health_failed=0
if ! curl -fsS --max-time 5 "${BACKEND_BASE}/health" >/dev/null; then
  health_failed=1
fi
if [ "$health_failed" -ne 0 ]; then
  if ! curl -fsS --max-time 5 "${API_BASE}/api/v1/dashboard/provider" >/dev/null; then
    echo "health check 실패: $BACKEND_BASE/health, $API_BASE/api/v1/dashboard/provider" >&2
    exit 1
  fi
fi

echo "[2/5] API provider"
curl -fsS --max-time 5 "${API_BASE}/api/v1/dashboard/provider" >/dev/null

ok=0
req=0
network_err=0
service_err=0
other=0

for i in $(seq 1 "${ITERATIONS}"); do
  req=$((req+1))
  body_file="$(mktemp)"
  code=$(curl -sS -m "${TIMEOUT}" -o "${body_file}" -w '%{http_code}' \
    -H 'Content-Type: application/json' \
    -d "{\"client_message_id\":\"verify-${i}-$(date +%s%N)\",\"content\":\"연결검증 샘플\"}" \
    "${API_BASE}/api/v1/chat/runs" || true)

  if [ "${code}" = "200" ]; then
    status=$(jq -r '.status // ""' "${body_file}")
    echo "[$i] 200 status=${status}"
    ok=$((ok+1))
  else
    if [ -s "${body_file}" ]; then
      detail=$(jq -r '.error.code // .code // ""' "${body_file}" 2>/dev/null || true)
      if [ "${detail}" = "EXTRACTION_TIMEOUT" ] || [ "${detail}" = "EXTRACTION_CONCURRENCY_EXCEEDED" ] || [ "${detail}" = "EXTRACTION_PROVIDER_FAILED" ]; then
        service_err=$((service_err+1))
      else
        other=$((other+1))
      fi
      echo "[$i] HTTP ${code}, code=${detail}"
    else
      network_err=$((network_err+1))
      echo "[$i] HTTP ${code}, network/연결 실패"
    fi
  fi
  rm -f "${body_file}"
  sleep 0.1
done

if [ "$STRESS_TEST" != "0" ]; then
  echo "[3/5] 동시 연결 부담 테스트 (${STRESS_TEST}건)"
  temp_dir="$(mktemp -d)"

  for i in $(seq 1 "${STRESS_TEST}"); do
    payload="{\\\"client_message_id\\\":\\\"stress-${i}-$(date +%s%N)\\\",\\\"content\\\":\\\"연결점검 동시요청\\\"}"
    payload_file="${temp_dir}/payload-${i}.json"
    code_file="${temp_dir}/code-${i}.txt"
    body_file="${temp_dir}/body-${i}.json"
    printf '%s' "${payload}" > "${payload_file}"
    (
      curl -sS -m 20 -H 'Content-Type: application/json' -d "@${payload_file}" \
        "${API_BASE}/api/v1/chat/runs" -o "${body_file}" -w '%{http_code}' > "${code_file}" || true
    ) >/dev/null 2>&1 &
  done

  wait

  stress_ok=0
  stress_network=0
  stress_service=0
  stress_other=0

  for i in $(seq 1 "${STRESS_TEST}"); do
    code_file="${temp_dir}/code-${i}.txt"
    body_file="${temp_dir}/body-${i}.json"
    code="$(cat "${code_file}" 2>/dev/null || echo 000)"
    if [ "${code}" = "200" ]; then
      stress_ok=$((stress_ok+1))
      continue
    fi

    if [ ! -s "${body_file}" ]; then
      stress_network=$((stress_network+1))
      echo "[stress ${i}] ${code}: network/연결 실패"
      continue
    fi

    detail=$(python3 - "${body_file}" <<'PY'
import json
import sys
try:
    data = json.load(open(sys.argv[1]))
    print(data.get('error', {}).get('code', '') or data.get('code', ''))
except Exception:
    print('')
PY
)

    if [ "${detail}" = "EXTRACTION_TIMEOUT" ] || [ "${detail}" = "EXTRACTION_CONCURRENCY_EXCEEDED" ] || [ "${detail}" = "EXTRACTION_PROVIDER_FAILED" ]; then
      stress_service=$((stress_service+1))
    else
      stress_other=$((stress_other+1))
    fi
    echo "[stress ${i}] ${code} code=${detail}"
  done

  echo "동시요청 결과: 성공=${stress_ok}, 네트워크오류=${stress_network}, 서버오류=${stress_service}, 기타=${stress_other}/${STRESS_TEST}"
  rm -rf "${temp_dir}"
fi

echo "[4/5] 결과"
echo "요청=${req}, 성공=${ok}, 네트워크오류=${network_err}, 서버오류=${service_err}, 기타=${other}"

if [ "$ok" -eq 0 ]; then
  echo "FAIL: 연속 실패. 브라우저는 다른 기기/주소(127.0.0.1:3100 vs 192.168.x.x:3100)와 캐시를 확인하세요." >&2
  exit 2
fi

if [ "${network_err}" -gt "0" ]; then
  if [ "$TIMEOUT" -le 5 ]; then
    echo "INFO: 현재 TIMEOUT=${TIMEOUT}초로 설정되어 있어 처리 지연이 긴 요청이 네트워크 오류로 보일 수 있습니다." >&2
  else
    echo "WARN: 네트워크 오류가 관찰되었습니다. 프록시/방화벽/주소 불일치 가능성 점검하세요." >&2
  fi
fi

echo "[5/5] Dashboard page check"
if curl -fsS --max-time 5 "http://127.0.0.1:${DASHBOARD_PORT_HINT}" >/dev/null; then
  echo "dashboard ok"
else
  echo "dashboard access check failed" >&2
fi

echo "connectivity verify done"
