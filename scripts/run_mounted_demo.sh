#!/usr/bin/env bash
set -euo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
export FRONTEND_DIST="${FRONTEND_DIST:-${ROOT}/apps/razorsharp-concept/dist-mounted}"
export BROWSER_DEMO_ENABLED=true
export OPERATOR_DEMO_OPEN_ACCESS=true
export BROWSER_ORIGIN="${BROWSER_ORIGIN:-http://localhost:${PORT:-8000}}"
export WORKER_HEALTH_URL="${WORKER_HEALTH_URL:-http://127.0.0.1:${WORKER_HEALTH_PORT:-8001}/healthz}"
export VOICE_GATEWAY_PORT="${VOICE_GATEWAY_PORT:-${VOICE_PORT:-8100}}"
export VOICE_GATEWAY_URL="${VOICE_GATEWAY_URL:-http://127.0.0.1:${VOICE_GATEWAY_PORT}}"
export VOICE_PUBLIC_ORIGIN="${VOICE_PUBLIC_ORIGIN:-http://127.0.0.1:${VOICE_GATEWAY_PORT}}"
export VOICE_GATEWAY_ALLOWED_ORIGINS="${VOICE_GATEWAY_ALLOWED_ORIGINS:-${BROWSER_ORIGIN},http://localhost:${PORT:-8000},http://127.0.0.1:${PORT:-8000},http://localhost:3000,http://127.0.0.1:3000}"
if [ ! -f "${FRONTEND_DIST}/index.html" ]; then
  (cd "${ROOT}/apps/razorsharp-concept" && npm run build:mounted)
fi
exec bash "${ROOT}/scripts/run_demo.sh" "$@"
