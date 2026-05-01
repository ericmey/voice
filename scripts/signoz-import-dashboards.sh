#!/usr/bin/env bash
#
# Import the SigNoz LiveKit dashboard (and any other JSON files in
# ops/signoz/dashboards/) into the locally-running SigNoz instance.
#
# Reads ops/signoz/dashboards/*.json. Each file is POSTed to
# /api/v1/dashboards on http://localhost:8080. The first run creates
# the dashboard; subsequent runs are no-ops because SigNoz dedupes by
# title (you'll see "dashboard already exists" in stderr — safe to
# ignore).
#
# Auth: requires the admin user from the first-run UI signup. Pass
# credentials via env:
#   SIGNOZ_USER=ericmey@... SIGNOZ_PASS=... make signoz-import-dashboards
# or set them in your shell rc.
#
# Source of dashboards: https://github.com/SigNoz/dashboards
# To pull the latest from upstream:
#   curl -fsSL "https://raw.githubusercontent.com/SigNoz/dashboards/main/livekit/livekit-dashboard.json" \
#     -o ops/signoz/dashboards/livekit-dashboard.json

set -euo pipefail

SIGNOZ_URL="${SIGNOZ_URL:-http://localhost:8080}"
DASH_DIR="${SIGNOZ_DASHBOARD_DIR:-ops/signoz/dashboards}"

if [[ -z "${SIGNOZ_USER:-}" || -z "${SIGNOZ_PASS:-}" ]]; then
  echo "[signoz-import] need SIGNOZ_USER + SIGNOZ_PASS env vars (admin from first-run UI signup)" >&2
  echo "[signoz-import] example: SIGNOZ_USER=you@example.com SIGNOZ_PASS=... make signoz-import-dashboards" >&2
  exit 1
fi

echo "[signoz-import] logging into ${SIGNOZ_URL} as ${SIGNOZ_USER}"
TOKEN=$(curl -fsSL -X POST "${SIGNOZ_URL}/api/v1/login" \
  -H 'content-type: application/json' \
  -d "{\"email\":\"${SIGNOZ_USER}\",\"password\":\"${SIGNOZ_PASS}\"}" \
  | python3 -c "import sys,json; print(json.load(sys.stdin)['accessJwt'])")

if [[ -z "${TOKEN}" ]]; then
  echo "[signoz-import] login failed — check SIGNOZ_USER / SIGNOZ_PASS" >&2
  exit 2
fi

shopt -s nullglob
files=("${DASH_DIR}"/*.json)
if [[ ${#files[@]} -eq 0 ]]; then
  echo "[signoz-import] no dashboard JSON files in ${DASH_DIR}" >&2
  exit 3
fi

for f in "${files[@]}"; do
  echo "[signoz-import] POST $(basename "${f}")"
  HTTP_CODE=$(curl -sS -o /tmp/signoz-import-resp -w '%{http_code}' \
    -X POST "${SIGNOZ_URL}/api/v1/dashboards" \
    -H "authorization: Bearer ${TOKEN}" \
    -H 'content-type: application/json' \
    --data @"${f}")
  case "${HTTP_CODE}" in
    2*) echo "[signoz-import]   imported (HTTP ${HTTP_CODE})" ;;
    409) echo "[signoz-import]   already exists (HTTP 409) — skipping" ;;
    *)  echo "[signoz-import]   FAILED (HTTP ${HTTP_CODE}):"; cat /tmp/signoz-import-resp; echo ;;
  esac
done

echo "[signoz-import] done. Open ${SIGNOZ_URL}/dashboards"
