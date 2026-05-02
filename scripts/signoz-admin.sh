#!/usr/bin/env bash
#
# Thin curl wrapper for SigNoz admin API calls. Autoloads
# secrets/signoz.env (gitignored) so the API key never has to live in
# shell history.
#
# Usage:
#   scripts/signoz-admin.sh GET  /api/v1/dashboards
#   scripts/signoz-admin.sh POST /api/v1/dashboards @ops/signoz/dashboards/livekit-dashboard.json
#   scripts/signoz-admin.sh DELETE /api/v1/dashboards/<uuid>
#   scripts/signoz-admin.sh GET  /api/v1/services
#   scripts/signoz-admin.sh GET  /api/v1/version
#
# The script:
#   1. Sources secrets/signoz.env (which exports SIGNOZ_API_KEY + SIGNOZ_URL).
#   2. Forwards METHOD + PATH to curl with the auth header attached.
#   3. Pretty-prints JSON via python3 -m json.tool, falls back to raw on parse error.
#   4. Exits non-zero if the HTTP code is >= 400.

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "${SCRIPT_DIR}/.." && pwd)"
SECRETS_FILE="${REPO_ROOT}/secrets/signoz.env"

if [[ -f "${SECRETS_FILE}" ]]; then
    set -a
    # shellcheck disable=SC1090
    source "${SECRETS_FILE}"
    set +a
fi

SIGNOZ_URL="${SIGNOZ_URL:-http://localhost:8080}"

if [[ -z "${SIGNOZ_API_KEY:-}" ]]; then
    echo "[signoz-admin] no SIGNOZ_API_KEY in env or ${SECRETS_FILE}" >&2
    echo "[signoz-admin] copy config/signoz.env.example → secrets/signoz.env and fill it in" >&2
    exit 1
fi

if [[ $# -lt 2 ]]; then
    echo "usage: $0 <METHOD> <PATH> [body | @file]" >&2
    exit 2
fi

METHOD="$1"; shift
URL_PATH="$1"; shift
BODY="${1:-}"

curl_args=(
    -sS
    -X "${METHOD}"
    -H "SIGNOZ-API-KEY: ${SIGNOZ_API_KEY}"
    -H 'content-type: application/json'
    -o /tmp/signoz-admin-resp
    -w '%{http_code}'
)

if [[ -n "${BODY}" ]]; then
    if [[ "${BODY}" == @* ]]; then
        curl_args+=(--data-binary "${BODY}")
    else
        curl_args+=(--data "${BODY}")
    fi
fi

HTTP_CODE=$(curl "${curl_args[@]}" "${SIGNOZ_URL}${URL_PATH}")

# Print body (pretty-printed if JSON, raw otherwise).
if python3 -m json.tool /tmp/signoz-admin-resp 2>/dev/null; then
    :
else
    cat /tmp/signoz-admin-resp
    echo
fi

# Echo final status.
case "${HTTP_CODE}" in
    2*) echo "# HTTP ${HTTP_CODE}" >&2; exit 0 ;;
    *)  echo "# HTTP ${HTTP_CODE}" >&2; exit 1 ;;
esac
