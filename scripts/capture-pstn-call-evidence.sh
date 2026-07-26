#!/usr/bin/env bash
#
# Arm and collect a private evidence bundle around one real Sumi PSTN call.
# This script is read-only with respect to the running stack. It does not place
# a call, restart a container, truncate logs, or change routing.
#
# Usage (run on the deployment host from the voice repo):
#   scripts/capture-pstn-call-evidence.sh arm [label]
#   scripts/capture-pstn-call-evidence.sh collect
#   scripts/capture-pstn-call-evidence.sh status
#
# The bundle can contain caller identity and transcripts. It is written outside
# the repo under ~/voice-call-evidence with mode 0700 and must not be committed.

set -euo pipefail

readonly ACTION="${1:-}"
readonly LABEL="${2:-sumi-pstn}"
readonly VOICE_LOGS="${LIVEKIT_VOICE_LOGS:-$HOME/voice/logs/voice}"
readonly EVIDENCE_ROOT="${VOICE_PSTN_EVIDENCE_ROOT:-$HOME/voice-call-evidence}"
readonly ARM_FILE="$EVIDENCE_ROOT/.armed"

readonly CONTAINERS=(
  voice-livekit-sip
  voice-livekit-server
  voice-agent-sumi
  voicebook-stream
  parakeet-ctl
  sumi-local-llm
)

usage() {
  sed -n '2,12p' "$0" >&2
  exit 2
}

require_command() {
  command -v "$1" >/dev/null 2>&1 || {
    echo "required command not found: $1" >&2
    exit 2
  }
}

container_snapshot() {
  local output="$1"
  : >"$output"
  for container in "${CONTAINERS[@]}"; do
    if docker inspect "$container" >/dev/null 2>&1; then
      docker inspect "$container" | python3 -c '
import json
import sys

item = json.load(sys.stdin)[0]
state = item.get("State") or {}
health = (state.get("Health") or {}).get("Status", "none")
print(
    f"{item.get('"'"'Name'"'"', '"'"'/'"'"')} "
    f"id={item.get('"'"'Id'"'"', '"'"'unknown'"'"')} "
    f"started={state.get('"'"'StartedAt'"'"', '"'"'unknown'"'"')} "
    f"status={state.get('"'"'Status'"'"', '"'"'unknown'"'"')} "
    f"health={health} "
    f"restarts={item.get('"'"'RestartCount'"'"', '"'"'unknown'"'"')} "
    f"image={(item.get('"'"'Config'"'"') or {}).get('"'"'Image'"'"', '"'"'unknown'"'"')}"
)
' >>"$output"
    else
      printf '/%s MISSING\n' "$container" >>"$output"
    fi
  done
}

arm() {
  if [[ ! "$LABEL" =~ ^[A-Za-z0-9._-]+$ ]]; then
    echo "label may contain only letters, numbers, dot, underscore, and hyphen" >&2
    exit 2
  fi
  mkdir -p "$EVIDENCE_ROOT"
  chmod 0700 "$EVIDENCE_ROOT"
  if [[ -e "$ARM_FILE" ]]; then
    echo "already armed: $ARM_FILE" >&2
    echo "collect the existing boundary before arming a new one" >&2
    exit 3
  fi

  local started_at epoch bundle
  started_at="$(date -u +%Y-%m-%dT%H:%M:%SZ)"
  epoch="$(date +%s)"
  bundle="$EVIDENCE_ROOT/${started_at//:/-}-$LABEL"
  mkdir -m 0700 "$bundle"

  printf '%s\n%s\n%s\n' "$started_at" "$epoch" "$bundle" >"$ARM_FILE"
  chmod 0600 "$ARM_FILE"
  container_snapshot "$bundle/containers-before.txt"
  find "$VOICE_LOGS/call-telemetry" "$VOICE_LOGS/phone-transcripts" \
    -maxdepth 1 -type f -print 2>/dev/null | sort >"$bundle/files-before.txt" || true
  if [[ -f "$VOICE_LOGS/call-manifest.jsonl" ]]; then
    wc -l <"$VOICE_LOGS/call-manifest.jsonl" | tr -d ' ' >"$bundle/manifest-lines-before.txt"
  else
    echo 0 >"$bundle/manifest-lines-before.txt"
  fi

  {
    echo "armed_at_utc=$started_at"
    echo "armed_at_epoch=$epoch"
    echo "host=$(hostname)"
    echo "voice_logs=$VOICE_LOGS"
    echo "bundle=$bundle"
    echo "privacy=may contain caller identity and transcripts; do not commit"
  } >"$bundle/README.txt"

  echo "armed at $started_at"
  echo "bundle: $bundle"
}

status() {
  if [[ ! -e "$ARM_FILE" ]]; then
    echo "not armed"
    exit 1
  fi
  local started_at epoch bundle
  mapfile -t arm_state <"$ARM_FILE"
  started_at="${arm_state[0]}"
  epoch="${arm_state[1]}"
  bundle="${arm_state[2]}"
  echo "armed_at_utc=$started_at"
  echo "armed_at_epoch=$epoch"
  echo "bundle=$bundle"
}

collect() {
  if [[ ! -e "$ARM_FILE" ]]; then
    echo "not armed; run '$0 arm' before the call" >&2
    exit 3
  fi

  local started_at epoch bundle container safe_name
  mapfile -t arm_state <"$ARM_FILE"
  started_at="${arm_state[0]}"
  epoch="${arm_state[1]}"
  bundle="${arm_state[2]}"
  mkdir -p "$bundle/logs" "$bundle/call-telemetry" "$bundle/phone-transcripts"
  chmod 0700 "$bundle" "$bundle/logs" "$bundle/call-telemetry" "$bundle/phone-transcripts"

  container_snapshot "$bundle/containers-after.txt"
  docker ps --format '{{.Names}}\t{{.Status}}\t{{.Image}}' >"$bundle/docker-ps-after.txt"

  for container in "${CONTAINERS[@]}"; do
    safe_name="${container//\//_}"
    if docker inspect "$container" >/dev/null 2>&1; then
      docker logs --timestamps --since "$started_at" "$container" \
        >"$bundle/logs/$safe_name.log" 2>&1 || true
    else
      printf 'container missing at collection: %s\n' "$container" \
        >"$bundle/logs/$safe_name.log"
    fi
  done

  find "$VOICE_LOGS/call-telemetry" -maxdepth 1 -type f -newermt "@$epoch" \
    -exec cp -p {} "$bundle/call-telemetry/" \; 2>/dev/null || true
  find "$VOICE_LOGS/phone-transcripts" -maxdepth 1 -type f -newermt "@$epoch" \
    -exec cp -p {} "$bundle/phone-transcripts/" \; 2>/dev/null || true

  if [[ -f "$VOICE_LOGS/call-manifest.jsonl" ]]; then
    local manifest_lines_before
    manifest_lines_before="$(<"$bundle/manifest-lines-before.txt")"
    tail -n "+$((manifest_lines_before + 1))" "$VOICE_LOGS/call-manifest.jsonl" \
      >"$bundle/call-manifest-after.jsonl"
  fi

  {
    echo "collected_at_utc=$(date -u +%Y-%m-%dT%H:%M:%SZ)"
    echo "telemetry_files=$(find "$bundle/call-telemetry" -maxdepth 1 -type f | wc -l | tr -d ' ')"
    echo "transcript_files=$(find "$bundle/phone-transcripts" -maxdepth 1 -type f | wc -l | tr -d ' ')"
    echo
    echo "new_call_ids:"
    find "$bundle/call-telemetry" -maxdepth 1 -type f -name '*.json' -exec basename {} .json \; | sort
    echo
    echo "worker_correlation:"
    grep -E 'entrypoint: room=|caller resolved:|\[TRANSCRIPT:|llm turn end:|closing agent session' \
      "$bundle/logs/voice-agent-sumi.log" || true
    echo
    echo "sip_transport:"
    grep -Ei 'using codecs|rtp|packet|jitter|call.*(ended|closed|disconnected)|bye|cause' \
      "$bundle/logs/voice-livekit-sip.log" || true
  } >"$bundle/SUMMARY.txt"

  (cd "$bundle" && find . -type f ! -name SHA256SUMS -print0 | sort -z | xargs -0 sha256sum) \
    >"$bundle/SHA256SUMS"
  rm -f "$ARM_FILE"

  echo "collected boundary $started_at -> $(date -u +%Y-%m-%dT%H:%M:%SZ)"
  echo "bundle: $bundle"
  echo "summary: $bundle/SUMMARY.txt"
}

require_command docker
require_command python3
case "$ACTION" in
  arm) arm ;;
  collect) collect ;;
  status) status ;;
  *) usage ;;
esac
