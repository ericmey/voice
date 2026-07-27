#!/usr/bin/env bash
#
# Exit-non-zero if anything in the voice stack is unhealthy. Meant to be
# cron-runnable or wired into a Discord webhook.
#
# Deployment reality (2026-07-10): the stack runs as Docker Compose on
# mizuki (Ubuntu). The four voice agents are containers (voice-agent-<name>),
# not launchd jobs, and their "registered worker" signal is on container
# stdout (docker logs), not a log file. This script keys off Docker state
# and Redis, so it works on the host it actually runs on.
#
# Checks:
#   1. docker         — the daemon is reachable
#   2. redis          — voice-redis answers PING
#   3. livekit-server — container up + :7880 responds
#   4. livekit-sip    — container up (network_mode: host, no docker port map)
#   5. livekit-egress — container up
#   6. agents         — each DEPLOYED voice-agent-<name> up, low restart count,
#                       and a "registered worker" line in its logs. Agents that
#                       are planned but not deployed report "skip", never fail —
#                       and get the full check automatically once they exist.
#   7. voicebook      — TTS /healthz ready on loopback :5056
#   8. local-llm      — llama.cpp /health on loopback :8088
#   9. parakeet       — Riva ready+live on loopback :9000
#  10. SIP routing    — sip_inbound_trunk + sip_dispatch_rule present in Redis
#
# 2026-07-27: the original AGENTS list required nyla/aoi/yua/party — seats that
# were never deployed — and did not check agent-sumi, voicebook, the LLM, or
# Parakeet at all. Result: guaranteed-red every cron run for weeks, and blind to
# the actual production path. A check that is always red is worse than no check;
# a real failure cannot be seen inside it.
#
# Exits 0 if all green, 1 if any check failed.
#
# Usage:
#   scripts/health-check.sh              # normal
#   scripts/health-check.sh --quiet      # only emit on failure (cron-friendly)
#   scripts/health-check.sh --json       # machine-readable output
set -u  # no -e: we want to run every check

QUIET=false
FORMAT=text

while [[ $# -gt 0 ]]; do
  case "$1" in
    --quiet) QUIET=true ;;
    --json)  FORMAT=json ;;
    *) echo "unknown flag: $1" >&2; exit 2 ;;
  esac
  shift
done

# Required = deployed today. Optional = planned seats: skip while absent,
# fully checked the moment their container exists.
AGENTS=(sumi)
OPTIONAL_AGENTS=(nyla aoi yua party)
# On mizuki the health-check user may not be in the docker group; fall back
# to sudo (cron typically runs as root or a docker-group user, where the
# bare docker works and this branch is never taken).
if docker ps >/dev/null 2>&1; then
  DOCKER=(docker)
elif sudo -n docker ps >/dev/null 2>&1; then
  DOCKER=(sudo docker)
else
  DOCKER=(docker)  # let the first real call surface the permission error
fi

# Bounded: a wedged agent that restarted a handful of times across a long
# uptime is fine; a crash-loop is not. Flag above this.
MAX_RESTARTS="${VOICE_HEALTH_MAX_RESTARTS:-5}"

failed=0
declare -a RESULTS

record() {
  local name="$1" status="$2" detail="$3"
  RESULTS+=("${name}|${status}|${detail}")
  [[ "$status" == "ok" || "$status" == "skip" ]] || failed=$((failed + 1))
}

_container_up() { [[ "$("${DOCKER[@]}" inspect -f '{{.State.Running}}' "$1" 2>/dev/null)" == "true" ]]; }
_restart_count() { "${DOCKER[@]}" inspect -f '{{.RestartCount}}' "$1" 2>/dev/null || echo "?"; }

# ---- docker daemon -------------------------------------------------
if "${DOCKER[@]}" ps >/dev/null 2>&1; then
  record "docker" ok "daemon reachable"
else
  record "docker" fail "cannot reach docker daemon (permission or daemon down)"
  # Nothing else is checkable without docker — emit and bail.
  printf '%-18s %-6s %s\n' CHECK STATUS DETAIL
  for r in "${RESULTS[@]}"; do IFS='|' read -r n s d <<<"$r"; printf '%-18s %-6s %s\n' "$n" "$s" "$d"; done
  exit 1
fi

# ---- redis ---------------------------------------------------------
if [[ "$("${DOCKER[@]}" exec voice-redis redis-cli ping 2>/dev/null)" == "PONG" ]]; then
  record "redis" ok "PONG"
else
  record "redis" fail "no PONG from voice-redis"
fi

# ---- livekit-server ------------------------------------------------
if _container_up voice-livekit-server; then
  if "${DOCKER[@]}" exec voice-livekit-server wget -q -O /dev/null --timeout=3 http://127.0.0.1:7880/ 2>/dev/null \
    || curl -fsS --max-time 3 http://127.0.0.1:7880/ >/dev/null 2>&1; then
    record "livekit-server" ok "up, :7880 responding"
  else
    record "livekit-server" fail "container up but :7880 not responding"
  fi
else
  record "livekit-server" fail "container not running"
fi

# ---- livekit-sip / egress ------------------------------------------
for svc in livekit-sip livekit-egress; do
  if _container_up "voice-${svc}"; then
    record "${svc}" ok "container up"
  else
    record "${svc}" fail "container not running"
  fi
done

# ---- agents --------------------------------------------------------
_agent_ok_patterns='registered worker|worker started|connected to server'
_check_agent() {  # $1=name  $2=required|optional
  local a="$1" mode="$2" c="voice-agent-$1" restarts reg_line worker_id
  if ! _container_up "$c"; then
    if [[ "$mode" == "required" ]]; then
      record "agent-${a}" fail "container not running"
    else
      record "agent-${a}" skip "not deployed (checked automatically once it exists)"
    fi
    return
  fi
  restarts="$(_restart_count "$c")"
  reg_line="$("${DOCKER[@]}" logs "$c" 2>&1 | grep -E "${_agent_ok_patterns}" | tail -1)"
  if [[ -z "${reg_line}" ]]; then
    record "agent-${a}" fail "up (restarts=${restarts}) but no worker-registration line in logs"
  elif [[ "${restarts}" =~ ^[0-9]+$ ]] && (( restarts > MAX_RESTARTS )); then
    record "agent-${a}" fail "registered but restarts=${restarts} > ${MAX_RESTARTS} (crash-loop?)"
  else
    worker_id="$(echo "${reg_line}" | grep -oE '"id": "[^"]+"' | head -1 | cut -d'"' -f4)"
    record "agent-${a}" ok "up, registered worker=${worker_id:-unknown} restarts=${restarts}"
  fi
}
for a in "${AGENTS[@]}";          do _check_agent "$a" required; done
for a in "${OPTIONAL_AGENTS[@]}"; do _check_agent "$a" optional; done

# ---- voicebook / local-llm / parakeet (loopback HTTP contracts) ----
if curl -fsS --max-time 4 http://127.0.0.1:5056/healthz 2>/dev/null | grep -q '"ready":true'; then
  record "voicebook" ok "healthz ready on :5056"
else
  record "voicebook" fail "no ready healthz on :5056"
fi
if curl -fsS --max-time 4 http://127.0.0.1:8088/health >/dev/null 2>&1; then
  record "local-llm" ok "llama.cpp /health on :8088"
else
  record "local-llm" fail "no /health on :8088"
fi
if curl -fsS --max-time 4 http://127.0.0.1:9000/v1/health/ready >/dev/null 2>&1 \
   && curl -fsS --max-time 4 http://127.0.0.1:9000/v1/health/live >/dev/null 2>&1; then
  record "parakeet" ok "ready+live on :9000"
else
  record "parakeet" fail "ready/live not both 200 on :9000"
fi

# ---- SIP routing (persisted in Redis) ------------------------------
present="$("${DOCKER[@]}" exec voice-redis redis-cli exists sip_inbound_trunk sip_dispatch_rule 2>/dev/null | tr -d '[:space:]')"
if [[ "${present}" == "2" ]]; then
  record "sip-routing" ok "sip_inbound_trunk + sip_dispatch_rule present"
else
  record "sip-routing" fail "routing state missing in Redis (present=${present}/2); inbound calls will fail — run scripts/register-sip-routing.sh"
fi

# ---- emit ----------------------------------------------------------
if [[ "$FORMAT" == "json" ]]; then
  items_json="["
  first=true
  for r in "${RESULTS[@]}"; do
    IFS='|' read -r name status detail <<<"$r"
    $first && first=false || items_json+=","
    items_json+="{\"name\":\"$name\",\"status\":\"$status\",\"detail\":\"$detail\"}"
  done
  items_json+="]"
  printf '{"failed":%d,"checks":%s}\n' "$failed" "$items_json"
else
  if [[ $failed -eq 0 ]] && $QUIET; then
    exit 0
  fi
  printf '%-18s %-6s %s\n' CHECK STATUS DETAIL
  for r in "${RESULTS[@]}"; do
    IFS='|' read -r name status detail <<<"$r"
    color=0
    [[ "$status" == "ok"   ]] && color=32
    [[ "$status" == "fail" ]] && color=31
    [[ "$status" == "skip" ]] && color=33
    printf '%-18s \033[1;%dm%-6s\033[0m %s\n' "$name" "$color" "$status" "$detail"
  done
  if [[ $failed -gt 0 ]]; then
    printf '\n\033[1;31m%d check(s) failed.\033[0m\n' "$failed"
  fi
fi

exit $([[ $failed -eq 0 ]] && echo 0 || echo 1)
