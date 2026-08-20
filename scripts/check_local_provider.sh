#!/bin/zsh
set -euo pipefail

OLLAMA_URL="${LOCAL_LLM_BASE_URL:-http://127.0.0.1:11434}"
MODEL="${LOCAL_LLM_MODEL:-qwen3.5:35b-a3b-q4_K_M}"
BACKEND_URL="${BACKEND_URL:-http://127.0.0.1:8100}"

echo "[1/3] Ollama health"
curl -fsS --max-time 5 "$OLLAMA_URL/api/tags" >/dev/null
echo "ok: $OLLAMA_URL"

echo "[2/3] Model availability"
if ! curl -fsS --max-time 5 "$OLLAMA_URL/api/tags" | grep -q '"name":"'"$MODEL"'"'; then
  echo "model not found: $MODEL" >&2
  echo "run: ollama pull $MODEL" >&2
  exit 1
fi
echo "ok: $MODEL"

echo "[3/3] BY backend health"
curl -fsS --max-time 5 "$BACKEND_URL/health"
echo
echo "Local Provider is ready. API Provider is not used by this check."
