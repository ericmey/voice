#!/usr/bin/env bash
#
# Follow all agent logs with a color-coded prefix. Ctrl-C to stop.
#
# Usage:
#   scripts/tail-logs.sh                  # all agents
#   scripts/tail-logs.sh nyla aoi         # subset
#   scripts/tail-logs.sh --grep tool=     # filter to lines matching pattern

set -uo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
LOG_DIR="${LIVEKIT_VOICE_LOGS:-${REPO_ROOT}/logs/voice}"
FILTER=""

agents=()
while [[ $# -gt 0 ]]; do
  case "$1" in
    --grep) FILTER="$2"; shift 2 ;;
    nyla|aoi|yua|party) agents+=("$1"); shift ;;
    *) echo "unknown arg: $1" >&2; exit 1 ;;
  esac
done
if [[ ${#agents[@]} -eq 0 ]]; then
  agents=(nyla aoi yua party)
fi

agent_color() {
  # Per-agent ANSI color for the log prefix. Case statement for bash 3.2
  # compatibility on stock macOS.
  case "$1" in
    nyla)  printf '\033[1;35m' ;;  # magenta
    aoi)   printf '\033[1;36m' ;;  # cyan
    yua)   printf '\033[1;32m' ;;  # green
    party) printf '\033[1;33m' ;;  # yellow
    *)     printf '\033[0m'    ;;
  esac
}
RESET="\033[0m"

pids=()
cleanup() {
  for p in "${pids[@]}"; do kill "$p" 2>/dev/null || true; done
}
trap cleanup EXIT INT TERM

for a in "${agents[@]}"; do
  color="$(agent_color "$a")"
  log="${LOG_DIR}/agent-${a}.log"
  if [[ ! -f "$log" ]]; then
    echo "no log at $log (agent not deployed yet?)" >&2
    continue
  fi
  if [[ -n "$FILTER" ]]; then
    tail -n 0 -F "$log" | grep --line-buffered -E "$FILTER" | while IFS= read -r line; do
      printf "${color}[%s]${RESET} %s\n" "$a" "$line"
    done &
  else
    tail -n 0 -F "$log" | while IFS= read -r line; do
      printf "${color}[%s]${RESET} %s\n" "$a" "$line"
    done &
  fi
  pids+=("$!")
done

wait
