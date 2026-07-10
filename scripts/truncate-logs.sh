#!/usr/bin/env bash
#
# Clear the per-call log artifacts under $LIVEKIT_VOICE_LOGS for a clean test
# baseline: transcripts, per-call telemetry JSON, recordings, the post-call
# memory log, and the call manifest. Agent stdout is Docker-managed
# (docker logs) and is NOT touched here.
#
# Best-effort: artifacts are written by the agent/egress containers (uid 1001),
# so clearing them from the host may require running as that uid or via sudo —
# failures warn and are skipped rather than aborting.
#
# Usage:
#   scripts/truncate-logs.sh

set -uo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
LOG_DIR="${LIVEKIT_VOICE_LOGS:-${REPO_ROOT}/logs/voice}"

log() { printf "\033[1;34m[truncate]\033[0m %s\n" "$*"; }
warn() { printf "\033[1;33m[warn]\033[0m %s\n" "$*"; }

# Per-call artifact dirs: remove contents, keep the dir.
for sub in phone-transcripts call-telemetry recordings; do
  d="${LOG_DIR}/${sub}"
  if [[ -d "$d" ]]; then
    if find "$d" -mindepth 1 -delete 2>/dev/null; then
      log "cleared ${sub}/"
    else
      warn "could not clear ${sub}/ (permission? try sudo)"
    fi
  fi
done

# Append-only logs: truncate in place.
for f in postcall-memory.log call-manifest.jsonl; do
  path="${LOG_DIR}/${f}"
  if [[ -f "$path" ]]; then
    if : >"$path" 2>/dev/null; then
      log "truncated ${f}"
    else
      warn "could not truncate ${f} (permission? try sudo)"
    fi
  fi
done

log "clean baseline under ${LOG_DIR}"
