#!/usr/bin/env bash
#
# Render launchd plists from the template + secrets file and install them.
# Idempotent: safe to re-run. Reloading an agent sends SIGTERM and waits
# for LiveKit to drain the old worker before launchd starts the
# replacement, so deploys do not intentionally interrupt active calls.
#
# Usage:
#   scripts/deploy-agents.sh                  # all agents
#   scripts/deploy-agents.sh nyla             # one agent
#   scripts/deploy-agents.sh nyla aoi yua     # subset

set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
TEMPLATE="${REPO_ROOT}/config/launchd/ai.openclaw.livekit-agent.plist.template"
SECRETS="${OPENCLAW_SECRETS:-${REPO_ROOT}/secrets/livekit-agents.env}"
LAUNCH_AGENTS_DIR="${HOME}/Library/LaunchAgents"

log()  { printf "\033[1;34m[deploy]\033[0m %s\n" "$*"; }
warn() { printf "\033[1;33m[warn]\033[0m   %s\n" "$*"; }
die()  { printf "\033[1;31m[fatal]\033[0m  %s\n" "$*" >&2; exit 1; }

abs_path() {
  case "$1" in
    /*) printf "%s\n" "$1" ;;
    *)  printf "%s/%s\n" "${REPO_ROOT}" "$1" ;;
  esac
}

default_openclaw_bin() {
  if [[ -x "${HOME}/.openclaw/bin/openclaw" ]]; then
    printf "%s/.openclaw/bin/openclaw\n" "${HOME}"
    return
  fi
  if command -v openclaw >/dev/null 2>&1; then
    command -v openclaw
    return
  fi
  printf "/opt/homebrew/bin/openclaw\n"
}

default_service_version() {
  git -C "${REPO_ROOT}" rev-parse --short HEAD 2>/dev/null || printf "dev\n"
}

# ---- preflight -------------------------------------------------------
[[ -r "${TEMPLATE}" ]] || die "template not found: ${TEMPLATE}"
[[ -r "${SECRETS}"  ]] || die "secrets file not found: ${SECRETS} (copy config/secrets.env.example and fill in)"

# Load secrets into the current env so envsubst can see them.
# shellcheck disable=SC1090
set -a; . "${SECRETS}"; set +a

VOICE_LOGS="$(abs_path "${LIVEKIT_VOICE_LOGS:-${REPO_ROOT}/logs/voice}")"
OPENCLAW_BIN="${OPENCLAW_BIN:-$(default_openclaw_bin)}"
OPENCLAW_SERVICE_VERSION="${OPENCLAW_SERVICE_VERSION:-$(default_service_version)}"
LIVEKIT_AGENT_DRAIN_WAIT_SECONDS="${LIVEKIT_AGENT_DRAIN_WAIT_SECONDS:-1860}"
LIVEKIT_AGENT_EXIT_TIMEOUT="${LIVEKIT_AGENT_EXIT_TIMEOUT:-${LIVEKIT_AGENT_DRAIN_WAIT_SECONDS}}"
LIVEKIT_EGRESS_HOST_RECORDINGS_DIR="$(
  abs_path "${LIVEKIT_EGRESS_HOST_RECORDINGS_DIR:-${VOICE_LOGS}/recordings}"
)"
mkdir -p "${VOICE_LOGS}" "${LAUNCH_AGENTS_DIR}"

: "${LIVEKIT_URL:?LIVEKIT_URL missing from ${SECRETS}}"
: "${LIVEKIT_API_KEY:?LIVEKIT_API_KEY missing from ${SECRETS}}"
: "${LIVEKIT_API_SECRET:?LIVEKIT_API_SECRET missing from ${SECRETS}}"
: "${GOOGLE_API_KEY:?GOOGLE_API_KEY missing from ${SECRETS}}"
: "${GATEWAY_AUTH_TOKEN:?GATEWAY_AUTH_TOKEN missing from ${SECRETS}}"
: "${DISCORD_TOKEN_NYLA:?DISCORD_TOKEN_NYLA missing from ${SECRETS}}"
: "${DISCORD_TOKEN_AOI:?DISCORD_TOKEN_AOI missing from ${SECRETS}}"
: "${DISCORD_TOKEN_YUA:?DISCORD_TOKEN_YUA missing from ${SECRETS}}"
: "${MUSUBI_V2_BASE_URL:?MUSUBI_V2_BASE_URL missing from ${SECRETS}}"
: "${MUSUBI_V2_TOKEN_NYLA:?MUSUBI_V2_TOKEN_NYLA missing from ${SECRETS}}"
: "${MUSUBI_V2_TOKEN_AOI:?MUSUBI_V2_TOKEN_AOI missing from ${SECRETS}}"
: "${MUSUBI_V2_TOKEN_YUA:?MUSUBI_V2_TOKEN_YUA missing from ${SECRETS}}"
[[ -x "${OPENCLAW_BIN}" ]] || die "OPENCLAW_BIN is not executable: ${OPENCLAW_BIN}"
case "${LIVEKIT_AGENT_DRAIN_WAIT_SECONDS}" in
  ''|*[!0-9]*) die "LIVEKIT_AGENT_DRAIN_WAIT_SECONDS must be an integer" ;;
esac
case "${LIVEKIT_AGENT_EXIT_TIMEOUT}" in
  ''|*[!0-9]*) die "LIVEKIT_AGENT_EXIT_TIMEOUT must be an integer" ;;
esac
case "${OPENCLAW_OTEL_ENABLED:-true}" in
  1|[Tt][Rr][Uu][Ee]|[Yy][Ee][Ss])
    : "${OPENCLAW_OTLP_ENDPOINT:?OPENCLAW_OTLP_ENDPOINT missing from ${SECRETS} while OPENCLAW_OTEL_ENABLED is true}"
    ;;
esac

# Agents to deploy (default: all). Build the array from positional
# args, or fall back to all agents if none were given — the explicit $#
# check is `set -u`-safe while `"${@}"` with zero args is not.
if [[ $# -eq 0 ]]; then
  agents=(nyla aoi yua party)
else
  agents=("$@")
fi

needs_party_keys=false
for agent in "${agents[@]}"; do
  if [[ "${agent}" == "party" ]]; then
    needs_party_keys=true
  fi
done
if [[ "${needs_party_keys}" == "true" ]]; then
  : "${OPENAI_API_KEY:?OPENAI_API_KEY missing from ${SECRETS} (Party uses Whisper STT)}"
  : "${ELEVENLABS_API_KEY:?ELEVENLABS_API_KEY missing from ${SECRETS} (Party uses ElevenLabs TTS)}"
fi

agent_label() {
  # Human-readable description for the plist Comment field. Case
  # statement instead of an associative array so this works on
  # macOS's stock bash 3.2.
  case "$1" in
    nyla)  echo "phone-nyla (Gemini 2.5 Flash Native Audio)" ;;
    aoi)   echo "phone-aoi (Gemini 2.5 Flash Native Audio)" ;;
    yua)   echo "phone-yua (Gemini 2.5 Flash Native Audio)" ;;
    party) echo "phone-party (chained STT/LLM/TTS)" ;;
    *)     die "unknown agent: $1" ;;
  esac
}

agent_discord_token() {
  # Per-agent Discord bot identity. Nyla is the orchestrator; Party
  # reuses Nyla's token.
  # If Party ever gets her own bot identity, add DISCORD_TOKEN_PARTY
  # to the preflight checks below and map it here.
  case "$1" in
    nyla|party) echo "${DISCORD_TOKEN_NYLA}" ;;
    aoi)        echo "${DISCORD_TOKEN_AOI}"  ;;
    yua)        echo "${DISCORD_TOKEN_YUA}"  ;;
    *)          die "no discord token mapping for: $1" ;;
  esac
}

agent_musubi_token() {
  # Per-agent Musubi bearer (HS256 JWT, 1yr exp). Party reuses Nyla's
  # token because Party's AgentConfig mirrors Nyla's namespace — same
  # presence, same scope. If Party ever forks its identity, mint a
  # separate token and add MUSUBI_V2_TOKEN_PARTY to preflight +
  # mapping here.
  case "$1" in
    nyla|party) echo "${MUSUBI_V2_TOKEN_NYLA}" ;;
    aoi)        echo "${MUSUBI_V2_TOKEN_AOI}"  ;;
    yua)        echo "${MUSUBI_V2_TOKEN_YUA}"  ;;
    *)          die "no musubi token mapping for: $1" ;;
  esac
}

render_plist() {
  local agent="$1"
  local label
  label="$(agent_label "$agent")"
  local discord_token
  discord_token="$(agent_discord_token "$agent")"
  [[ -n "${discord_token}" ]] || die "discord token for ${agent} is empty — check secrets file"
  local musubi_token
  musubi_token="$(agent_musubi_token "$agent")"
  [[ -n "${musubi_token}" ]] || die "musubi token for ${agent} is empty — check secrets file"
  local out="${LAUNCH_AGENTS_DIR}/ai.openclaw.livekit-agent-${agent}.plist"

  # sed-based render. envsubst would swallow any $... in paths; explicit
  # token substitution is safer.
  sed \
    -e "s|{{AGENT_NAME}}|${agent}|g" \
    -e "s|{{AGENT_LABEL}}|${label}|g" \
    -e "s|{{MONOREPO_ROOT}}|${REPO_ROOT}|g" \
    -e "s|{{LIVEKIT_VOICE_LOGS}}|${VOICE_LOGS}|g" \
    -e "s|{{LIVEKIT_AGENT_EXIT_TIMEOUT}}|${LIVEKIT_AGENT_EXIT_TIMEOUT}|g" \
    -e "s|{{HOME}}|${HOME}|g" \
    -e "s|{{LIVEKIT_URL}}|${LIVEKIT_URL}|g" \
    -e "s|{{LIVEKIT_API_KEY}}|${LIVEKIT_API_KEY}|g" \
    -e "s|{{LIVEKIT_API_SECRET}}|${LIVEKIT_API_SECRET}|g" \
    -e "s|{{GOOGLE_API_KEY}}|${GOOGLE_API_KEY}|g" \
    -e "s|{{GATEWAY_AUTH_TOKEN}}|${GATEWAY_AUTH_TOKEN}|g" \
    -e "s|{{GATEWAY_PORT}}|${GATEWAY_PORT:-18789}|g" \
    -e "s|{{OPENCLAW_HOOK_TOKEN}}|${OPENCLAW_HOOK_TOKEN:-}|g" \
    -e "s|{{OPENCLAW_GATEWAY_HTTP_URL}}|${OPENCLAW_GATEWAY_HTTP_URL:-http://127.0.0.1:${GATEWAY_PORT:-18789}}|g" \
    -e "s|{{OPENCLAW_HOOKS_PATH}}|${OPENCLAW_HOOKS_PATH:-/hooks}|g" \
    -e "s|{{DISCORD_BOT_TOKEN}}|${discord_token}|g" \
    -e "s|{{MUSUBI_V2_BASE_URL}}|${MUSUBI_V2_BASE_URL}|g" \
    -e "s|{{MUSUBI_V2_TOKEN}}|${musubi_token}|g" \
    -e "s|{{OPENCLAW_BIN}}|${OPENCLAW_BIN}|g" \
    -e "s|{{OPENAI_API_KEY}}|${OPENAI_API_KEY:-}|g" \
    -e "s|{{ELEVENLABS_API_KEY}}|${ELEVENLABS_API_KEY:-}|g" \
    -e "s|{{OPENCLAW_OTEL_ENABLED}}|${OPENCLAW_OTEL_ENABLED:-true}|g" \
    -e "s|{{OPENCLAW_OTEL_DEBUG}}|${OPENCLAW_OTEL_DEBUG:-false}|g" \
    -e "s|{{OPENCLAW_OTEL_VERBOSE}}|${OPENCLAW_OTEL_VERBOSE:-false}|g" \
    -e "s|{{OPENCLAW_OTEL_HTTP_INSTRUMENTATION}}|${OPENCLAW_OTEL_HTTP_INSTRUMENTATION:-true}|g" \
    -e "s|{{OPENCLAW_DEPLOYMENT_ENVIRONMENT}}|${OPENCLAW_DEPLOYMENT_ENVIRONMENT:-local}|g" \
    -e "s|{{OPENCLAW_SERVICE_VERSION}}|${OPENCLAW_SERVICE_VERSION}|g" \
    -e "s|{{OPENCLAW_OTLP_ENDPOINT}}|${OPENCLAW_OTLP_ENDPOINT:-}|g" \
    -e "s|{{OPENCLAW_OTLP_HEADERS}}|${OPENCLAW_OTLP_HEADERS:-}|g" \
    -e "s|{{OPENCLAW_OTEL_LOGS_ENABLED}}|${OPENCLAW_OTEL_LOGS_ENABLED:-true}|g" \
    -e "s|{{OPENCLAW_OTLP_LOGS_ENDPOINT}}|${OPENCLAW_OTLP_LOGS_ENDPOINT:-}|g" \
    -e "s|{{OPENCLAW_OTLP_LOGS_HEADERS}}|${OPENCLAW_OTLP_LOGS_HEADERS:-}|g" \
    -e "s|{{OPENCLAW_OTEL_METRICS_ENABLED}}|${OPENCLAW_OTEL_METRICS_ENABLED:-true}|g" \
    -e "s|{{OPENCLAW_OTLP_METRICS_ENDPOINT}}|${OPENCLAW_OTLP_METRICS_ENDPOINT:-}|g" \
    -e "s|{{OPENCLAW_OTLP_METRICS_HEADERS}}|${OPENCLAW_OTLP_METRICS_HEADERS:-}|g" \
    -e "s|{{OPENCLAW_RECORD_AUDIO}}|${OPENCLAW_RECORD_AUDIO:-false}|g" \
    -e "s|{{LIVEKIT_EGRESS_HOST_RECORDINGS_DIR}}|${LIVEKIT_EGRESS_HOST_RECORDINGS_DIR}|g" \
    -e "s|{{LIVEKIT_EGRESS_CONTAINER_RECORDINGS_DIR}}|${LIVEKIT_EGRESS_CONTAINER_RECORDINGS_DIR:-/recordings}|g" \
    "${TEMPLATE}" > "${out}"

  log "rendered ${out}"
}

ensure_venv_ready() {
  # Single root venv for the entire workspace. `uv sync` at the repo
  # root installs sdk, tools, and every agent as editable workspace
  # members. Called once before the render loop.
  local python="${REPO_ROOT}/.venv/bin/python"
  if [[ ! -x "${python}" ]]; then
    log "syncing root workspace venv (first-time uv sync)"
    (cd "${REPO_ROOT}" && uv sync --all-groups)
  fi
}

launchctl_pid() {
  local label="$1"
  launchctl list "${label}" 2>/dev/null \
    | awk -F'= ' '/"PID"/ { gsub(/[;"]/, "", $2); print $2; exit }'
}

pid_alive() {
  local pid="$1"
  [[ -n "${pid}" ]] && kill -0 "${pid}" >/dev/null 2>&1
}

wait_for_pid_exit() {
  local label="$1"
  local pid="$2"
  local waited=0

  while pid_alive "${pid}"; do
    if (( waited >= LIVEKIT_AGENT_DRAIN_WAIT_SECONDS )); then
      return 1
    fi
    sleep 1
    waited=$((waited + 1))
  done

  log "${label} old pid ${pid} exited after ${waited}s"
  return 0
}

wait_for_running_pid() {
  local label="$1"
  local waited=0
  local pid

  while (( waited < 30 )); do
    pid="$(launchctl_pid "${label}")"
    if [[ -n "${pid}" ]]; then
      log "${label} running with pid ${pid}"
      return 0
    fi
    sleep 1
    waited=$((waited + 1))
  done

  return 1
}

reload_plist() {
  local agent="$1"
  local label="ai.openclaw.livekit-agent-${agent}"
  local path="${LAUNCH_AGENTS_DIR}/${label}.plist"
  local domain_target="gui/$(id -u)/${label}"
  local old_pid

  # Idempotent replace: disable KeepAlive restarts, ask the loaded worker
  # to drain via SIGTERM, wait for the old PID to exit, then bootout and
  # bootstrap the freshly rendered plist. Relying on bootout alone is not
  # enough: launchd may impose its own stop timeout before LiveKit's
  # drain_timeout elapses.
  if launchctl print "${domain_target}" >/dev/null 2>&1; then
    old_pid="$(launchctl_pid "${label}")"
    log "disabling ${label} while old worker drains"
    launchctl disable "${domain_target}" 2>/dev/null || true
    log "sending SIGTERM to ${label}; LiveKit may drain active jobs before exit"
    launchctl kill TERM "${domain_target}" 2>/dev/null || true
    if [[ -n "${old_pid}" ]]; then
      wait_for_pid_exit "${label}" "${old_pid}" \
        || die "${label} did not exit within ${LIVEKIT_AGENT_DRAIN_WAIT_SECONDS}s"
    fi
    launchctl bootout "gui/$(id -u)" "${path}" 2>/dev/null || true
  fi
  launchctl enable "${domain_target}" 2>/dev/null || true
  launchctl bootstrap "gui/$(id -u)" "${path}"
  wait_for_running_pid "${label}" || {
    warn "${label} did not start at bootstrap; kickstarting without force"
    launchctl kickstart "${domain_target}"
    wait_for_running_pid "${label}" || die "${label} did not start"
  }
  log "bootstrapped ${label}"
}

ensure_venv_ready
for agent in "${agents[@]}"; do
  case "$agent" in
    nyla|aoi|yua|party) ;;
    *) die "unknown agent: $agent (valid: nyla, aoi, yua, party)" ;;
  esac
  render_plist "$agent"
  reload_plist  "$agent"
done

log "done. tail logs with: scripts/tail-logs.sh"
