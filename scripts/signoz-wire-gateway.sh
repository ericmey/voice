#!/usr/bin/env bash
#
# Wire the OpenClaw Gateway's `diagnostics-otel` plugin into the local
# SigNoz stack so gateway traces, metrics, and logs land alongside the
# LiveKit-agent telemetry already exported from this repo.
#
# Why a script instead of hand-edits to ~/.openclaw/openclaw.json?
#   - That config file is the operator's source of truth for the
#     gateway. It is rewritten frequently by `openclaw config set`,
#     plugins, and self-healing logic. Hand edits get clobbered.
#   - `openclaw config set` is the documented, blessed CLI mechanism;
#     it preserves the rest of the config and keeps a `.bak` snapshot
#     for every write.
#   - Idempotent re-runs let the same script be used to verify, repair,
#     or reapply settings if the gateway config drifts.
#
# This script is a peer of scripts/signoz.sh — it does the gateway side
# of the wiring, while signoz.sh manages the SigNoz docker-compose
# stack. The LiveKit-agent side of the wiring is rendered into per-agent
# launchd plists by scripts/deploy-agents.sh.
#
# Usage:
#   scripts/signoz-wire-gateway.sh             # apply the config + restart
#   scripts/signoz-wire-gateway.sh --dry-run   # print the commands only
#   scripts/signoz-wire-gateway.sh --verify    # check current state, no writes
#
# Env overrides (rarely needed; sane defaults match the docs):
#   OPENCLAW_GW_OTLP_ENDPOINT  default http://localhost:4318
#   OPENCLAW_GW_SERVICE_NAME   default openclaw-gateway
#   OPENCLAW_GW_FLUSH_MS       default 60000   (gateway docs default)
#   OPENCLAW_GW_SAMPLE_RATE    default 1.0     (capture everything; 0..1)
#   OPENCLAW_GW_CAPTURE        default all
#                              all | tools_only | none
#                              "all"  -> input/output/tool I/O/system prompt
#                              "tools_only" -> tool I/O only
#                              "none" -> bounded identifiers only

set -euo pipefail

OTLP_ENDPOINT="${OPENCLAW_GW_OTLP_ENDPOINT:-http://localhost:4318}"
SERVICE_NAME="${OPENCLAW_GW_SERVICE_NAME:-openclaw-gateway}"
FLUSH_MS="${OPENCLAW_GW_FLUSH_MS:-60000}"
SAMPLE_RATE="${OPENCLAW_GW_SAMPLE_RATE:-1.0}"
CAPTURE_PROFILE="${OPENCLAW_GW_CAPTURE:-all}"

DRY_RUN="false"
VERIFY_ONLY="false"
for arg in "$@"; do
    case "${arg}" in
        --dry-run) DRY_RUN="true" ;;
        --verify)  VERIFY_ONLY="true" ;;
        -h|--help)
            sed -n '2,/^set -euo/p' "$0" | sed 's/^# \{0,1\}//;/^set -euo/d'
            exit 0
            ;;
        *)
            printf "[wire] unknown arg: %s\n" "${arg}" >&2
            exit 2
            ;;
    esac
done

log()   { printf "\033[1;34m[wire]\033[0m %s\n" "$*"; }
warn()  { printf "\033[1;33m[warn]\033[0m %s\n" "$*"; }
err()   { printf "\033[1;31m[err ]\033[0m %s\n" "$*" >&2; }
ok()    { printf "\033[1;32m[ ok ]\033[0m %s\n" "$*"; }

require_openclaw() {
    if ! command -v openclaw >/dev/null 2>&1; then
        err "\`openclaw\` CLI not on PATH (looked in: ${PATH%%:*})"
        err "Install it first; see https://docs.openclaw.ai/start/getting-started"
        exit 1
    fi
}

run() {
    if [[ "${DRY_RUN}" == "true" ]]; then
        printf "  + %s\n" "$*"
    else
        "$@"
    fi
}

# Inputs/outputs/system-prompt control. Three named profiles to keep the
# Makefile / docs simple — most operators want "all" (matches the LiveKit
# agent telemetry richness) or "none" (privacy-safe default).
case "${CAPTURE_PROFILE}" in
    all)         CAP_INPUT=true;  CAP_OUTPUT=true;  CAP_TOOL_IN=true;  CAP_TOOL_OUT=true;  CAP_SYS=true ;;
    tools_only)  CAP_INPUT=false; CAP_OUTPUT=false; CAP_TOOL_IN=true;  CAP_TOOL_OUT=true;  CAP_SYS=false ;;
    none)        CAP_INPUT=false; CAP_OUTPUT=false; CAP_TOOL_IN=false; CAP_TOOL_OUT=false; CAP_SYS=false ;;
    *)
        err "OPENCLAW_GW_CAPTURE must be one of: all | tools_only | none (got: ${CAPTURE_PROFILE})"
        exit 2
        ;;
esac
CAP_ENABLED=true
[[ "${CAPTURE_PROFILE}" == "none" ]] && CAP_ENABLED=false

require_openclaw

if [[ "${VERIFY_ONLY}" == "true" ]]; then
    log "current diagnostics.otel config:"
    openclaw config get diagnostics.otel || true
    log "plugins.allow:"
    openclaw config get plugins.allow || true
    log "plugin status (filter on diagnostics):"
    openclaw plugins list 2>/dev/null | grep -iE "diagnostics" || true
    exit 0
fi

log "wiring gateway -> ${OTLP_ENDPOINT} (service.name=${SERVICE_NAME}, capture=${CAPTURE_PROFILE})"

# 1) Diagnostics surface + OTLP exporter knobs.
run openclaw config set diagnostics.enabled true
run openclaw config set diagnostics.otel.enabled true
run openclaw config set diagnostics.otel.traces true
run openclaw config set diagnostics.otel.metrics true
run openclaw config set diagnostics.otel.logs true
run openclaw config set diagnostics.otel.protocol http/protobuf
run openclaw config set diagnostics.otel.endpoint "${OTLP_ENDPOINT}"
run openclaw config set diagnostics.otel.serviceName "${SERVICE_NAME}"
run openclaw config set diagnostics.otel.flushIntervalMs "${FLUSH_MS}"
run openclaw config set diagnostics.otel.sampleRate "${SAMPLE_RATE}"

# 2) Content capture profile. Each subkey is independently opt-in.
run openclaw config set diagnostics.otel.captureContent.enabled "${CAP_ENABLED}"
run openclaw config set diagnostics.otel.captureContent.inputMessages "${CAP_INPUT}"
run openclaw config set diagnostics.otel.captureContent.outputMessages "${CAP_OUTPUT}"
run openclaw config set diagnostics.otel.captureContent.toolInputs "${CAP_TOOL_IN}"
run openclaw config set diagnostics.otel.captureContent.toolOutputs "${CAP_TOOL_OUT}"
run openclaw config set diagnostics.otel.captureContent.systemPrompt "${CAP_SYS}"

# 3) Plugin allowlist. `openclaw plugins enable` is blocked for plugins
#    not on the allowlist; we add it via the config CLI rather than hand-
#    editing JSON. The list below mirrors the operator's current
#    plugins.allow with diagnostics-otel appended (idempotent — running
#    again with the plugin already present is a no-op rewrite).
if [[ "${DRY_RUN}" == "false" ]]; then
    current_allow="$(openclaw config get plugins.allow 2>/dev/null || echo '[]')"
    if printf '%s' "${current_allow}" | grep -q '"diagnostics-otel"'; then
        ok "diagnostics-otel already in plugins.allow"
    else
        log "appending diagnostics-otel to plugins.allow"
        merged="$(printf '%s' "${current_allow}" | python3 -c 'import json,sys
arr = json.load(sys.stdin) or []
if "diagnostics-otel" not in arr:
    arr.append("diagnostics-otel")
print(json.dumps(arr))')"
        openclaw config set plugins.allow "${merged}"
    fi
else
    log "(dry-run) skipping plugins.allow merge"
fi

# 4) Flip the plugin enabled bit.
run openclaw plugins enable diagnostics-otel || warn "plugin enable returned non-zero (may already be enabled)"

# 5) Restart the gateway via launchd if it is launchd-managed; otherwise
#    fall back to the openclaw CLI restart. launchd is preferred because
#    it preserves the service definition + log paths configured in the
#    plist instead of detaching into a child process.
if [[ "${DRY_RUN}" == "false" ]]; then
    if launchctl list 2>/dev/null | grep -q "ai.openclaw.gateway"; then
        log "restarting via launchctl kickstart -k gui/$(id -u)/ai.openclaw.gateway"
        launchctl kickstart -k "gui/$(id -u)/ai.openclaw.gateway" || warn "kickstart returned non-zero"
    else
        log "no ai.openclaw.gateway launchd entry; falling back to openclaw gateway restart"
        openclaw gateway restart || warn "gateway restart returned non-zero"
    fi
fi

ok "gateway wired -> ${OTLP_ENDPOINT}"
ok "service.name=${SERVICE_NAME}  flushIntervalMs=${FLUSH_MS}  sampleRate=${SAMPLE_RATE}  capture=${CAPTURE_PROFILE}"
log "tail logs with:    tail -f ~/.openclaw/logs/gateway.log"
log "verify in SigNoz:  open http://localhost:8080/services"
