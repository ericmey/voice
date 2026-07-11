#!/usr/bin/env bash
#
# Follow the voice agent logs (Docker) with a color-coded per-agent prefix.
# Ctrl-C to stop.
#
# Usage:
#   scripts/tail-logs.sh                  # all agents
#   scripts/tail-logs.sh nyla aoi         # subset
#   scripts/tail-logs.sh --grep tool=     # filter to lines matching pattern
#
# Agent stdout goes to Docker's json-file driver (docker logs), not a file, so
# this follows `docker logs -f voice-agent-<name>`. `make logs` covers the
# infra containers (server / sip / redis).

set -uo pipefail

FILTER=""
agents=()
while [[ $# -gt 0 ]]; do
  case "$1" in
    --grep) FILTER="$2"; shift 2 ;;
    nyla | aoi | yua | sumi) agents+=("$1"); shift ;;
    *) echo "unknown arg: $1" >&2; exit 1 ;;
  esac
done
if [[ ${#agents[@]} -eq 0 ]]; then
  agents=(nyla aoi yua sumi)
fi

# docker may need sudo if the caller isn't in the docker group.
if docker ps >/dev/null 2>&1; then DOCKER=(docker); else DOCKER=(sudo docker); fi

agent_color() {
  case "$1" in
    nyla) printf '\033[1;35m' ;;  # magenta
    aoi) printf '\033[1;36m' ;;   # cyan
    yua) printf '\033[1;32m' ;;   # green
    sumi) printf '\033[1;33m' ;; # yellow
    *) printf '\033[0m' ;;
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
  container="voice-agent-${a}"
  if ! "${DOCKER[@]}" ps --format '{{.Names}}' | grep -qx "$container"; then
    echo "no running container $container (agent not deployed yet?)" >&2
    continue
  fi
  if [[ -n "$FILTER" ]]; then
    "${DOCKER[@]}" logs -f --tail 0 "$container" 2>&1 \
      | grep --line-buffered -E "$FILTER" \
      | while IFS= read -r line; do printf "${color}[%s]${RESET} %s\n" "$a" "$line"; done &
  else
    "${DOCKER[@]}" logs -f --tail 0 "$container" 2>&1 \
      | while IFS= read -r line; do printf "${color}[%s]${RESET} %s\n" "$a" "$line"; done &
  fi
  pids+=("$!")
done

wait
