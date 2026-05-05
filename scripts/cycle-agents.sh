#!/usr/bin/env bash
#
# Gracefully restart one or more agents in place. Useful after a code
# change in the SDK or an agent persona — agents pick up the new files
# on restart without a full deploy.
#
# Usage:
#   scripts/cycle-agents.sh                  # all three
#   scripts/cycle-agents.sh nyla aoi         # subset
#
# LiveKit drains active jobs on SIGTERM/SIGINT. Do not use
# `launchctl kickstart -k` as the normal path: it can interrupt a worker
# while its job process pool is still shutting down, which shows up in
# Loki as "Cannot close a process while it is still running".

set -euo pipefail

log() { printf "\033[1;34m[cycle]\033[0m %s\n" "$*"; }
warn() { printf "\033[1;33m[warn]\033[0m  %s\n" "$*"; }
die() { printf "\033[1;31m[fatal]\033[0m %s\n" "$*" >&2; exit 1; }

DRAIN_WAIT_SECONDS="${LIVEKIT_AGENT_DRAIN_WAIT_SECONDS:-1860}"
FORCE_ON_TIMEOUT="${LIVEKIT_AGENT_FORCE_ON_TIMEOUT:-false}"

case "${DRAIN_WAIT_SECONDS}" in
  ''|*[!0-9]*) die "LIVEKIT_AGENT_DRAIN_WAIT_SECONDS must be an integer" ;;
esac

agent_pid() {
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
    if (( waited >= DRAIN_WAIT_SECONDS )); then
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
  local old_pid="${2:-}"
  local waited=0
  local pid

  while (( waited < 30 )); do
    pid="$(agent_pid "${label}")"
    if [[ -n "${pid}" && "${pid}" != "${old_pid}" ]]; then
      log "${label} running with pid ${pid}"
      return 0
    fi
    sleep 1
    waited=$((waited + 1))
  done

  return 1
}

if [[ $# -eq 0 ]]; then
  agents=(nyla aoi party)
else
  agents=("$@")
fi

for agent in "${agents[@]}"; do
  label="ai.openclaw.livekit-agent-${agent}"
  target="gui/$(id -u)/${label}"

  case "${agent}" in
    nyla|aoi|party) ;;
    *) die "unknown agent: ${agent} (valid: nyla, aoi, party)" ;;
  esac

  if ! launchctl print "${target}" >/dev/null 2>&1; then
    die "${label} is not loaded; run make deploy first"
  fi

  old_pid="$(agent_pid "${label}")"
  if [[ -z "${old_pid}" ]]; then
    warn "${label} has no current pid; asking launchd to start it"
    launchctl kickstart "${target}"
    wait_for_running_pid "${label}" || die "${label} did not start"
    continue
  fi

  log "sending SIGTERM to ${target} (old pid ${old_pid}); LiveKit will drain active jobs"
  launchctl kill TERM "${target}"

  if ! wait_for_pid_exit "${label}" "${old_pid}"; then
    if [[ "${FORCE_ON_TIMEOUT}" == "true" ]]; then
      warn "${label} did not exit within ${DRAIN_WAIT_SECONDS}s; forcing restart"
      launchctl kickstart -k "${target}"
    else
      die "${label} did not exit within ${DRAIN_WAIT_SECONDS}s; set LIVEKIT_AGENT_FORCE_ON_TIMEOUT=true to force"
    fi
  fi

  if ! wait_for_running_pid "${label}" "${old_pid}"; then
    warn "${label} did not auto-restart; kickstarting without force"
    launchctl kickstart "${target}"
    wait_for_running_pid "${label}" "${old_pid}" || die "${label} did not restart"
  fi
done

log "done. run scripts/health-check.sh to confirm all requested agents re-registered."
