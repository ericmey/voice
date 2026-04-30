#!/usr/bin/env bash
#
# Verify the LangSmith OTel endpoint accepts our credentials BEFORE
# cycling agents. Cheap pre-flight: posts a single empty OTel span batch
# and reports the HTTP status.
#
# Reads OTEL_EXPORTER_OTLP_ENDPOINT and OTEL_EXPORTER_OTLP_HEADERS from
# secrets/livekit-agents.env (or whatever OPENCLAW_SECRETS points at).
#
# Exit codes:
#   0   endpoint reachable, key accepted (HTTP 2xx or 4xx with JSON body)
#   1   credentials missing
#   2   endpoint unreachable / auth rejected

set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
SECRETS="${OPENCLAW_SECRETS:-${REPO_ROOT}/secrets/livekit-agents.env}"

log()  { printf "\033[1;34m[trace-check]\033[0m %s\n" "$*"; }
warn() { printf "\033[1;33m[warn]\033[0m         %s\n" "$*"; }
die()  { printf "\033[1;31m[fatal]\033[0m        %s\n" "$*" >&2; exit 1; }

[[ -r "${SECRETS}" ]] || die "secrets file not found: ${SECRETS}"

# Source the file in a subshell so we don't pollute the caller's env.
# shellcheck disable=SC1090
set -a
source "${SECRETS}"
set +a

if [[ "${LANGSMITH_TRACING:-false}" != "true" ]]; then
  warn "LANGSMITH_TRACING=${LANGSMITH_TRACING:-unset} — tracing is disabled. Continuing the probe anyway."
fi

if [[ -z "${OTEL_EXPORTER_OTLP_ENDPOINT:-}" ]]; then
  die "OTEL_EXPORTER_OTLP_ENDPOINT not set"
fi
if [[ -z "${OTEL_EXPORTER_OTLP_HEADERS:-}" ]]; then
  die "OTEL_EXPORTER_OTLP_HEADERS not set"
fi

log "endpoint: ${OTEL_EXPORTER_OTLP_ENDPOINT}"

# Build curl -H args from the comma-separated header list.
header_args=()
IFS=',' read -ra header_pairs <<< "${OTEL_EXPORTER_OTLP_HEADERS}"
for pair in "${header_pairs[@]}"; do
  # Trim leading/trailing whitespace
  pair="${pair#"${pair%%[![:space:]]*}"}"
  pair="${pair%"${pair##*[![:space:]]}"}"
  header_args+=("-H" "${pair/=/:}")  # turn key=value into key:value
done

# OTel HTTP/protobuf wants a Content-Type. We post empty body — we are
# checking auth + reachability only, not actually exporting a span.
header_args+=("-H" "Content-Type: application/x-protobuf")

# Probe the /v1/traces sub-path on the OTel base endpoint.
probe_url="${OTEL_EXPORTER_OTLP_ENDPOINT%/}/v1/traces"
log "probing: ${probe_url}"

http_code=$(curl -s -o /tmp/trace-check-resp -w "%{http_code}" -X POST "${header_args[@]}" --data-binary "" "${probe_url}" || true)

log "HTTP ${http_code}"
if [[ "${http_code}" =~ ^[2-3] ]]; then
  log "✅ endpoint accepted credentials"
  exit 0
elif [[ "${http_code}" == "400" ]]; then
  # 400 on empty body is the EXPECTED response — auth passed, server
  # rejected the empty payload. That's a green light for tracing.
  log "✅ endpoint reachable and credentials valid (HTTP 400 = empty body, expected)"
  exit 0
elif [[ "${http_code}" == "401" || "${http_code}" == "403" ]]; then
  warn "credentials rejected — verify x-api-key in OTEL_EXPORTER_OTLP_HEADERS"
  exit 2
else
  warn "unexpected status — body:"
  head -c 500 /tmp/trace-check-resp 2>/dev/null || true
  echo
  exit 2
fi
