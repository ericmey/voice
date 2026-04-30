#!/usr/bin/env bash
#
# Render launchd plists from the template + secrets file and install them.
# Idempotent: safe to re-run. Reloading an agent kickstarts it, which
# picks up code and env changes without a reinstall.
#
# Usage:
#   scripts/deploy-agents.sh                  # all three agents
#   scripts/deploy-agents.sh nyla             # one agent
#   scripts/deploy-agents.sh nyla aoi party   # subset

set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
TEMPLATE="${REPO_ROOT}/config/launchd/ai.openclaw.livekit-agent.plist.template"
SECRETS="${OPENCLAW_SECRETS:-${REPO_ROOT}/secrets/livekit-agents.env}"
VOICE_LOGS="${LIVEKIT_VOICE_LOGS:-${REPO_ROOT}/logs/voice}"
OPENCLAW_BIN="${OPENCLAW_BIN:-/opt/homebrew/bin/openclaw}"
LAUNCH_AGENTS_DIR="${HOME}/Library/LaunchAgents"

log()  { printf "\033[1;34m[deploy]\033[0m %s\n" "$*"; }
warn() { printf "\033[1;33m[warn]\033[0m   %s\n" "$*"; }
die()  { printf "\033[1;31m[fatal]\033[0m  %s\n" "$*" >&2; exit 1; }

# ---- preflight -------------------------------------------------------
[[ -r "${TEMPLATE}" ]] || die "template not found: ${TEMPLATE}"
[[ -r "${SECRETS}"  ]] || die "secrets file not found: ${SECRETS} (copy config/secrets.env.example and fill in)"
mkdir -p "${VOICE_LOGS}" "${LAUNCH_AGENTS_DIR}"

# Load secrets into the current env so envsubst can see them.
# shellcheck disable=SC1090
set -a; . "${SECRETS}"; set +a

: "${LIVEKIT_URL:?LIVEKIT_URL missing from ${SECRETS}}"
: "${LIVEKIT_API_KEY:?LIVEKIT_API_KEY missing from ${SECRETS}}"
: "${LIVEKIT_API_SECRET:?LIVEKIT_API_SECRET missing from ${SECRETS}}"
: "${GOOGLE_API_KEY:?GOOGLE_API_KEY missing from ${SECRETS}}"
: "${GATEWAY_AUTH_TOKEN:?GATEWAY_AUTH_TOKEN missing from ${SECRETS}}"
: "${DISCORD_TOKEN_NYLA:?DISCORD_TOKEN_NYLA missing from ${SECRETS}}"
: "${DISCORD_TOKEN_AOI:?DISCORD_TOKEN_AOI missing from ${SECRETS}}"
: "${MUSUBI_V2_BASE_URL:?MUSUBI_V2_BASE_URL missing from ${SECRETS}}"
: "${MUSUBI_V2_TOKEN_NYLA:?MUSUBI_V2_TOKEN_NYLA missing from ${SECRETS}}"
: "${MUSUBI_V2_TOKEN_AOI:?MUSUBI_V2_TOKEN_AOI missing from ${SECRETS}}"
# Required by Party (chained STT/LLM/TTS pipeline). Other agents don't
# use them but they're always rendered into the plist for uniformity —
# fail loudly here so they don't crash silently at first call.
: "${OPENAI_API_KEY:?OPENAI_API_KEY missing from ${SECRETS} (Party uses Whisper STT)}"
: "${ELEVENLABS_API_KEY:?ELEVENLABS_API_KEY missing from ${SECRETS} (Party uses ElevenLabs TTS)}"

# Agents to deploy (default: all three). Build the array from positional
# args, or fall back to all three if none were given — the explicit $#
# check is `set -u`-safe while `"${@}"` with zero args is not.
if [[ $# -eq 0 ]]; then
  agents=(nyla aoi party)
else
  agents=("$@")
fi

agent_label() {
  # Human-readable description for the plist Comment field. Case
  # statement instead of an associative array so this works on
  # macOS's stock bash 3.2.
  case "$1" in
    nyla)  echo "phone-nyla (Gemini 2.5 Flash Native Audio)" ;;
    aoi)   echo "phone-aoi (Gemini 2.5 Flash Native Audio)" ;;
    party) echo "phone-party (chained STT/LLM/TTS)" ;;
    *)     die "unknown agent: $1" ;;
  esac
}

agent_discord_token() {
  # Per-agent Discord bot identity. Nyla is the orchestrator; Party
  # reuses Nyla's token so academy tools route through the same bot.
  # If Party ever gets her own bot identity, add DISCORD_TOKEN_PARTY
  # to the preflight checks below and map it here.
  case "$1" in
    nyla|party) echo "${DISCORD_TOKEN_NYLA}" ;;
    aoi)        echo "${DISCORD_TOKEN_AOI}"  ;;
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
    -e "s|{{HOME}}|${HOME}|g" \
    -e "s|{{LIVEKIT_URL}}|${LIVEKIT_URL}|g" \
    -e "s|{{LIVEKIT_API_KEY}}|${LIVEKIT_API_KEY}|g" \
    -e "s|{{LIVEKIT_API_SECRET}}|${LIVEKIT_API_SECRET}|g" \
    -e "s|{{GOOGLE_API_KEY}}|${GOOGLE_API_KEY}|g" \
    -e "s|{{GATEWAY_AUTH_TOKEN}}|${GATEWAY_AUTH_TOKEN}|g" \
    -e "s|{{DISCORD_BOT_TOKEN}}|${discord_token}|g" \
    -e "s|{{MUSUBI_V2_BASE_URL}}|${MUSUBI_V2_BASE_URL}|g" \
    -e "s|{{MUSUBI_V2_TOKEN}}|${musubi_token}|g" \
    -e "s|{{OPENCLAW_BIN}}|${OPENCLAW_BIN}|g" \
    -e "s|{{OPENAI_API_KEY}}|${OPENAI_API_KEY}|g" \
    -e "s|{{ELEVENLABS_API_KEY}}|${ELEVENLABS_API_KEY}|g" \
    -e "s|{{LANGSMITH_TRACING}}|${LANGSMITH_TRACING:-false}|g" \
    -e "s|{{OTEL_EXPORTER_OTLP_ENDPOINT}}|${OTEL_EXPORTER_OTLP_ENDPOINT:-}|g" \
    -e "s|{{OTEL_EXPORTER_OTLP_HEADERS}}|${OTEL_EXPORTER_OTLP_HEADERS:-}|g" \
    -e "s|{{LANGSMITH_PROCESSOR_DEBUG}}|${LANGSMITH_PROCESSOR_DEBUG:-false}|g" \
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

reload_plist() {
  local agent="$1"
  local label="ai.openclaw.livekit-agent-${agent}"
  local path="${LAUNCH_AGENTS_DIR}/${label}.plist"
  local domain_target="gui/$(id -u)/${label}"

  # Idempotent replace: bootout (if loaded), then bootstrap.
  if launchctl print "${domain_target}" >/dev/null 2>&1; then
    launchctl bootout "gui/$(id -u)" "${path}" 2>/dev/null || true
  fi
  launchctl bootstrap "gui/$(id -u)" "${path}"
  launchctl kickstart -k "${domain_target}"
  log "bootstrapped ${label}"
}

ensure_venv_ready
for agent in "${agents[@]}"; do
  case "$agent" in
    nyla|aoi|party) ;;
    *) die "unknown agent: $agent (valid: nyla, aoi, party)" ;;
  esac
  render_plist "$agent"
  reload_plist  "$agent"
done

log "done. tail logs with: scripts/tail-logs.sh"
