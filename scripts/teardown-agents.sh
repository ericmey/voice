#!/usr/bin/env bash
#
# Unload launchd agents and remove their plists. Non-destructive — the
# checked-out source and .venv/ stay put; only the launchd registrations
# and the rendered ~/Library/LaunchAgents/*.plist files are removed.
#
# Usage:
#   scripts/teardown-agents.sh                  # all agents
#   scripts/teardown-agents.sh nyla             # one

set -euo pipefail

LAUNCH_AGENTS_DIR="${HOME}/Library/LaunchAgents"

log()  { printf "\033[1;34m[teardown]\033[0m %s\n" "$*"; }

if [[ $# -eq 0 ]]; then
  agents=(nyla aoi yua party)
else
  agents=("$@")
fi

for agent in "${agents[@]}"; do
  label="ai.openclaw.livekit-agent-${agent}"
  path="${LAUNCH_AGENTS_DIR}/${label}.plist"
  target="gui/$(id -u)/${label}"

  if launchctl print "${target}" >/dev/null 2>&1; then
    launchctl bootout "gui/$(id -u)" "${path}" 2>/dev/null || true
    log "booted out ${label}"
  else
    log "${label} was not loaded"
  fi

  if [[ -f "${path}" ]]; then
    rm "${path}"
    log "removed ${path}"
  fi
done

log "done. source and .venv/ left in place. scripts/deploy-agents.sh to bring them back."
