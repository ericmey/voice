#!/usr/bin/env bash
#
# Truncate (not delete) all agent logs for a clean test baseline. Keeps
# file handles and permissions intact so launchd's open file descriptors
# keep working without a restart.
#
# Usage:
#   scripts/truncate-logs.sh

set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
LOG_DIR="${LIVEKIT_VOICE_LOGS:-${REPO_ROOT}/logs/voice}"

log() { printf "\033[1;34m[truncate]\033[0m %s\n" "$*"; }

count=0
for a in nyla aoi yua party; do
  for suffix in ".log" ".err.log"; do
    path="${LOG_DIR}/agent-${a}${suffix}"
    if [[ -f "$path" ]]; then
      : > "$path"
      count=$((count + 1))
    fi
  done
done

log "truncated ${count} file(s) under ${LOG_DIR}"
