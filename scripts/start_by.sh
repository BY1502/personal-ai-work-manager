#!/bin/zsh
set -euo pipefail

PROJECT_DIR="${BY_PROJECT_DIR:-$(cd "$(dirname "$0")/.." && pwd)}"
cd "$PROJECT_DIR"

docker compose up -d

for attempt in {1..30}; do
  if curl -fsS --max-time 2 http://127.0.0.1:8100/health >/dev/null 2>&1; then
    echo "BY is ready: http://127.0.0.1:3100"
    exit 0
  fi
  sleep 1
done

echo "BY containers started, but Backend health is not ready yet." >&2
echo "Check: docker compose ps && docker compose logs --tail=100 backend" >&2
exit 1
