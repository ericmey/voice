#!/usr/bin/env bash
#
# Import every JSON file in ops/signoz/dashboards/ into the local
# SigNoz instance. Each file is POSTed to /api/v1/dashboards. SigNoz
# dedupes by title, so a second run is a safe no-op (returns HTTP 409
# per file).
#
# Auth: two paths, in order of preference:
#
#   1. SIGNOZ_API_KEY (recommended)
#        Sends the request with the `SIGNOZ-API-KEY:` header. Generate
#        in the SigNoz UI under Settings → "API Keys" (or "Service
#        Accounts" on v0.130+) with the SigNoz-Admin role. Persist it
#        in secrets/signoz.env (gitignored, sourced by the make target).
#
#   2. SIGNOZ_USER + SIGNOZ_PASS (fallback)
#        Used when no API key is set. Logs in to /api/v1/login with the
#        admin email + password from the first-run UI signup, exchanges
#        for a short-lived JWT, and uses that.
#
# Configure once via secrets/signoz.env (see config/signoz.env.example).
# The Makefile target `make signoz-import-dashboards` autoloads that
# file before invoking this script.
#
# Source of dashboards: https://github.com/SigNoz/dashboards
# To pull the latest from upstream:
#   curl -fsSL "https://raw.githubusercontent.com/SigNoz/dashboards/main/livekit/livekit-dashboard.json" \
#     -o ops/signoz/dashboards/livekit-dashboard.json

set -euo pipefail

SIGNOZ_URL="${SIGNOZ_URL:-http://localhost:8080}"
DASH_DIR="${SIGNOZ_DASHBOARD_DIR:-ops/signoz/dashboards}"

auth_header=""
auth_method=""

if [[ -n "${SIGNOZ_API_KEY:-}" ]]; then
  auth_header="SIGNOZ-API-KEY: ${SIGNOZ_API_KEY}"
  auth_method="api-key"
elif [[ -n "${SIGNOZ_USER:-}" && -n "${SIGNOZ_PASS:-}" ]]; then
  echo "[signoz-import] no SIGNOZ_API_KEY — falling back to /api/v1/login as ${SIGNOZ_USER}"
  TOKEN=$(curl -fsSL -X POST "${SIGNOZ_URL}/api/v1/login" \
    -H 'content-type: application/json' \
    -d "{\"email\":\"${SIGNOZ_USER}\",\"password\":\"${SIGNOZ_PASS}\"}" \
    | python3 -c "import sys,json; print(json.load(sys.stdin)['accessJwt'])")
  if [[ -z "${TOKEN}" ]]; then
    echo "[signoz-import] login failed — check SIGNOZ_USER / SIGNOZ_PASS" >&2
    exit 2
  fi
  auth_header="authorization: Bearer ${TOKEN}"
  auth_method="user-pass"
else
  cat >&2 <<'EOF'
[signoz-import] no auth configured. set ONE of:

  SIGNOZ_API_KEY=<key>                  # generate in SigNoz UI → Settings → API Keys
  SIGNOZ_USER=<email> SIGNOZ_PASS=<pw>  # admin from first-run UI signup

The recommended path is to copy config/signoz.env.example to
secrets/signoz.env, fill in SIGNOZ_API_KEY, and run
`make signoz-import-dashboards` — the Makefile autoloads that file.
EOF
  exit 1
fi

# Verify auth is good before iterating files (avoids partial imports).
verify_code=$(curl -s -o /dev/null -w '%{http_code}' \
  -X GET "${SIGNOZ_URL}/api/v1/dashboards" \
  -H "${auth_header}")
case "${verify_code}" in
  2*) echo "[signoz-import] auth OK (${auth_method}, HTTP ${verify_code})" ;;
  401|403)
    echo "[signoz-import] auth REJECTED (HTTP ${verify_code}, method=${auth_method})" >&2
    if [[ "${auth_method}" == "api-key" ]]; then
      echo "[signoz-import]   the API key was rejected. Confirm the role is SigNoz-Admin and the key is not expired." >&2
    fi
    exit 4
    ;;
  *)  echo "[signoz-import] auth check returned HTTP ${verify_code} — proceeding anyway" ;;
esac

shopt -s nullglob
files=("${DASH_DIR}"/*.json)
if [[ ${#files[@]} -eq 0 ]]; then
  echo "[signoz-import] no dashboard JSON files in ${DASH_DIR}" >&2
  exit 3
fi

ok=0
skip=0
fail=0
for f in "${files[@]}"; do
  echo "[signoz-import] POST $(basename "${f}")"
  HTTP_CODE=$(curl -sS -o /tmp/signoz-import-resp -w '%{http_code}' \
    -X POST "${SIGNOZ_URL}/api/v1/dashboards" \
    -H "${auth_header}" \
    -H 'content-type: application/json' \
    --data @"${f}")
  case "${HTTP_CODE}" in
    2*)  echo "[signoz-import]   imported (HTTP ${HTTP_CODE})";  ((ok++)) ;;
    409) echo "[signoz-import]   already exists (HTTP 409) — skipping";  ((skip++)) ;;
    *)   echo "[signoz-import]   FAILED (HTTP ${HTTP_CODE}):"; cat /tmp/signoz-import-resp; echo;  ((fail++)) ;;
  esac
done

echo "[signoz-import] done — imported=${ok} skipped=${skip} failed=${fail}. Open ${SIGNOZ_URL}/dashboards"
[[ "${fail}" -eq 0 ]]
