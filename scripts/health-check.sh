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
#   6. agents         — each voice-agent-<name> up, low restart count,
#                       and a "registered worker" line in its logs
#   7. SIP routing     — sip_inbound_trunk + sip_dispatch_rule present in Redis
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

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
PATH="$HOME/.local/bin:$PATH"  # cron: uv/lk live here

# Overridable so the test suite can point the routing check at a fixture fleet and still run
# the REAL comparator. A monitor whose central check is unreachable from a test is a monitor
# nobody has ever seen go red.
CONFIG_DIR="${LIVEKIT_CONFIG_DIR:-${REPO_ROOT}/config}"
SECRETS_ENV="${VOICE_SECRETS_ENV:-${REPO_ROOT}/secrets/livekit-agents.env}"

AGENTS=(nyla aoi yua sumi)
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
  [[ "$status" == "ok" ]] || failed=$((failed + 1))
}

_container_up() { [[ "$("${DOCKER[@]}" inspect -f '{{.State.Running}}' "$1" 2>/dev/null)" == "true" ]]; }
_restart_count() { "${DOCKER[@]}" inspect -f '{{.RestartCount}}' "$1" 2>/dev/null || echo "?"; }

# Docker's OWN health verdict — the thing this script never read.
# Returns: healthy | unhealthy | starting | none
_health() {
  "${DOCKER[@]}" inspect -f '{{if .State.Health}}{{.State.Health.Status}}{{else}}none{{end}}' "$1" 2>/dev/null || echo "none"
}

# When did THIS container instance start? Registration proof must be bounded to it —
# a registration line from an earlier life proves nothing about the process running now.
_started_at() { "${DOCKER[@]}" inspect -f '{{.State.StartedAt}}' "$1" 2>/dev/null; }

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
for a in "${AGENTS[@]}"; do
  c="voice-agent-${a}"
  if ! _container_up "$c"; then
    record "agent-${a}" fail "container not running"
    continue
  fi
  restarts="$(_restart_count "$c")"
  health="$(_health "$c")"

  # DOCKER'S HEALTH VERDICT IS AUTHORITATIVE, and this script never read it.
  #
  # It checked: container running + ANY registration line anywhere in the log + restart count.
  # So an agent that registered and then WEDGED — up, not answering, health=unhealthy —
  # stayed green forever.
  #
  # And `restart: unless-stopped` does NOT save you: Docker restart policies act on container
  # EXIT, not on health status. An unhealthy container that keeps running is never restarted.
  # (A comment I wrote elsewhere claimed the opposite. It was wrong. Yua caught it.)
  #
  # So an unhealthy agent is not restarted AND was not reported. Both silences at once.
  if [[ "${health}" == "unhealthy" ]]; then
    record "agent-${a}" fail "UNHEALTHY (restarts=${restarts}) — up but its health endpoint is not answering. Docker will NOT restart it; restart policies act on exit, not health."
    continue
  fi
  if [[ "${health}" == "none" ]]; then
    record "agent-${a}" fail "no healthcheck defined — a wedged agent would be indistinguishable from a healthy one"
    continue
  fi
  if [[ "${health}" == "starting" ]]; then
    # NOT READY, and therefore not ok. `starting` IS bounded by Docker (once start_period
    # elapses, failing probes count and she flips to unhealthy) — but this script is a
    # readiness gate, and an agent still in prewarm cannot take a call. Reporting her green
    # would mean `make health` says yes at the exact moment the answer is "not yet".
    # Set VOICE_HEALTH_ALLOW_STARTING=1 during a deploy, when that is expected.
    if [[ "${VOICE_HEALTH_ALLOW_STARTING:-0}" == "1" ]]; then
      record "agent-${a}" ok "starting (prewarm; allowed by VOICE_HEALTH_ALLOW_STARTING)"
    else
      record "agent-${a}" fail "STARTING — not ready to take a call yet (prewarm). Not an error if you just deployed; re-run, or set VOICE_HEALTH_ALLOW_STARTING=1."
    fi
    continue
  fi

  # Registration proof, BOUNDED TO THIS CONTAINER'S CURRENT START.
  # `docker logs` without --since returns the whole life of the container, so a line from
  # before a wedge (or from a previous start after a restart) would still satisfy the check.
  started="$(_started_at "$c")"
  reg_line="$("${DOCKER[@]}" logs --since "${started}" "$c" 2>&1 | grep -E "${_agent_ok_patterns}" | tail -1)"
  if [[ -z "${reg_line}" ]]; then
    record "agent-${a}" fail "healthy but NO worker registration since this start (${started}) — she is not on LiveKit's roster; calls will not route to her"
  elif [[ "${restarts}" =~ ^[0-9]+$ ]] && (( restarts > MAX_RESTARTS )); then
    record "agent-${a}" fail "registered but restarts=${restarts} > ${MAX_RESTARTS} (crash-loop?)"
  else
    worker_id="$(echo "${reg_line}" | grep -oE '"id": "[^"]+"' | head -1 | cut -d'"' -f4)"
    record "agent-${a}" ok "healthy, registered worker=${worker_id:-unknown} restarts=${restarts}"
  fi
done

# ---- SIP routing: THE LIVE MAPPINGS, not "a key exists" -------------
#
# This used to be `redis-cli exists sip_inbound_trunk sip_dispatch_rule` == 2.
#
# That is a check that the routing table EXISTS. It is not a check that anyone is IN it.
# A dispatch hash holding three rules, or four rules all pointing at Nyla, or a rule whose
# agentName was typo'd — every one of those passes `exists`. And the failure they produce
# is the worst one we have: the call connects and Eric is talking to the wrong sister, or
# to silence. Nothing crashes. Nothing logs. The monitor stays green.
#
# So read the rules and require each of the four to actually be routable. LiveKit persists
# them protobuf-encoded, but the agentName literals are legible in the values, and it is
# those literals that inbound calls are routed on — so this checks the thing that decides
# who picks up.
if ! "${DOCKER[@]}" exec voice-redis redis-cli exists sip_inbound_trunk 2>/dev/null | grep -qx 1; then
  record "sip-trunk" fail "no inbound trunk in Redis — no call reaches us at all; run scripts/register-sip-routing.sh"
else
  record "sip-trunk" ok "inbound trunk present"
fi

# The live rules, compared EXACTLY against the validated candidate set — the same
# sdk.sip_preflight the registrar runs. The previous version of this greped `phone-[a-z]+`
# out of the protobuf blobs in Redis: format-fragile, and it could only ever answer "is this
# name present somewhere", never "is the mapping correct". A stale rule holding one of our
# DIDs passes a presence check. (Yua, round 2.)
_lk_env() {
  # LIVEKIT_API_KEY/SECRET are already in the rendered secrets file on disk, so this works
  # under cron with no 1Password prompt.
  set -a; . "${SECRETS_ENV}" 2>/dev/null || return 1; set +a
  export LIVEKIT_URL="${LIVEKIT_URL:-ws://localhost:7880}"
}

if ! command -v lk >/dev/null 2>&1 || ! command -v uv >/dev/null 2>&1; then
  # UNVERIFIABLE IS NOT VERIFIED. A monitor that cannot run its check reports that, loudly;
  # it does not stay quiet and let the absence read as health.
  record "sip-routing" fail "cannot verify routing — lk and/or uv not on PATH. This check did not run; that is not the same as passing."
elif ! ( _lk_env ) 2>/dev/null; then
  record "sip-routing" fail "cannot verify routing — ${SECRETS_ENV} is unreadable (no LiveKit credentials)"
else
  # THE AUTHORITATIVE QUERY MUST SUCCEED, and its exit status must be read INDEPENDENTLY.
  #
  # This was `lk ... | sdk.sip_preflight ...`. The script runs under `set -u`, NOT
  # `set -o pipefail`, so a pipeline's status is the LAST command's status — the comparator's.
  # If `lk` printed a complete but stale/cached/partial JSON document and then exited nonzero,
  # the comparator would happily validate that document and health would report green, having
  # never noticed that the query it based the verdict on had FAILED.
  #
  # A correct answer computed from an unreliable reading is not a correct answer. So: capture
  # lk's output and its status separately, and refuse to compare at all if the query failed.
  # (Yua, round 3.)
  live_json="$( ( _lk_env; lk sip dispatch list --json 2>/dev/null ) )"
  lk_status=$?

  if (( lk_status != 0 )); then
    record "sip-routing" fail "cannot verify routing — \`lk sip dispatch list\` exited ${lk_status}. The authoritative query FAILED; any JSON it printed is not trustworthy, and a verdict computed from it would be a guess wearing a checkmark."
  elif [[ -z "${live_json//[[:space:]]/}" ]]; then
    record "sip-routing" fail "cannot verify routing — \`lk sip dispatch list\` returned nothing"
  else
    routing_out="$( printf '%s' "${live_json}" \
      | ( cd "${REPO_ROOT}" && uv run python -m sdk.sip_preflight "${CONFIG_DIR}" --live - 2>&1 ) )"
    if (( $? == 0 )); then
      record "sip-routing" ok "live dispatch matches the validated set exactly (4 rules, no extras)"
    else
      record "sip-routing" fail "live dispatch does NOT match the validated set: $(tr '\n' ' ' <<<"${routing_out}" | sed 's/  */ /g')"
    fi
  fi
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
    # A HEARTBEAT, not silence.
    #
    # This used to `exit 0` printing nothing, so a healthy run left no trace. The cron log
    # was therefore EMPTY after ~250 runs — and an empty log means "everything is fine" and
    # "cron has been dead for a week" in exactly the same way. You cannot tell them apart,
    # which makes the monitor indistinguishable from a corpse. (It WAS running; I verified
    # 260 executions in syslog. But I could only verify that by going and looking, which is
    # the thing a monitor is supposed to save you from.)
    #
    # One compact line per healthy run. Now SILENCE MEANS BROKEN, which is the only reading
    # that is safe to act on. ~288 lines/day; logrotate handles it.
    printf '[%s] ok — %d checks passed\n' "$(date -Is)" "${#RESULTS[@]}"
    exit 0
  fi
  printf '%-18s %-6s %s\n' CHECK STATUS DETAIL
  for r in "${RESULTS[@]}"; do
    IFS='|' read -r name status detail <<<"$r"
    color=0
    [[ "$status" == "ok"   ]] && color=32
    [[ "$status" == "fail" ]] && color=31
    printf '%-18s \033[1;%dm%-6s\033[0m %s\n' "$name" "$color" "$status" "$detail"
  done
  if [[ $failed -gt 0 ]]; then
    printf '\n\033[1;31m%d check(s) failed.\033[0m\n' "$failed"
  fi
fi

exit $([[ $failed -eq 0 ]] && echo 0 || echo 1)
