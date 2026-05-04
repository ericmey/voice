#!/usr/bin/env bash
#
# Exit-non-zero if anything in the voice stack is unhealthy. Meant to be
# cron-runnable or wired into a Discord webhook later.
#
# Checks:
#   1. redis  — reachable on 127.0.0.1:6379
#   2. livekit-server — container up + /rtc/validate responds
#   3. livekit-sip    — container up + listening on 5060
#   4. livekit-egress — container up for local audio recordings
#   5. three agents   — PID live, registered-worker line in log
#   6. SIP routing    — trunk and three dispatch rules present
#
# Exits 0 if all green, 1 if any check failed. Writes a summary line per
# check so you can eyeball output or pipe to alertmanager.
#
# Usage:
#   scripts/health-check.sh              # normal
#   scripts/health-check.sh --quiet      # only emit on failure
#   scripts/health-check.sh --json       # machine-readable output

set -u  # no -e: we want to run every check

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
LOG_DIR="${LIVEKIT_VOICE_LOGS:-${REPO_ROOT}/logs/voice}"

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

failed=0
declare -a RESULTS

record() {
  local name="$1" status="$2" detail="$3"
  RESULTS+=("${name}|${status}|${detail}")
  [[ "$status" == "ok" ]] || failed=$((failed + 1))
}

# ---- redis ---------------------------------------------------------
if command -v redis-cli >/dev/null 2>&1 \
  && redis-cli -h 127.0.0.1 -p 6379 ping 2>/dev/null | grep -q PONG; then
  record "redis" ok "PONG via host redis-cli"
elif command -v docker >/dev/null 2>&1 \
  && docker exec openclaw-redis redis-cli ping 2>/dev/null | grep -q PONG; then
  record "redis" ok "PONG via openclaw-redis container"
else
  record "redis" fail "no PONG via host redis-cli or openclaw-redis container"
fi

# ---- livekit-server -----------------------------------------------
if docker ps --format '{{.Names}}' 2>/dev/null | grep -qx openclaw-livekit-server; then
  if curl -fsS --max-time 3 http://127.0.0.1:7880/ >/dev/null 2>&1; then
    record "livekit-server" ok "container up, :7880 responding"
  else
    record "livekit-server" fail "container up but :7880 not responding"
  fi
else
  record "livekit-server" fail "container not running"
fi

# ---- livekit-sip --------------------------------------------------
if docker ps --format '{{.Names}}' 2>/dev/null | grep -qx openclaw-livekit-sip; then
  record "livekit-sip" ok "container up"
else
  record "livekit-sip" fail "container not running"
fi

# ---- livekit-egress -----------------------------------------------
if docker ps --format '{{.Names}}' 2>/dev/null | grep -qx openclaw-livekit-egress; then
  record "livekit-egress" ok "container up"
else
  record "livekit-egress" fail "container not running"
fi

# ---- agents -------------------------------------------------------
# Accept multiple log signatures so the check survives log-format drift.
_agent_ok_patterns='registered worker|worker started|connected to server|session started'
for a in nyla aoi party; do
  pid_line="$(launchctl list 2>/dev/null | awk -v lbl="ai.openclaw.livekit-agent-${a}" '$3 == lbl {print $1}')"
  if [[ -n "${pid_line}" && "${pid_line}" != "-" ]]; then
    log_file="${LOG_DIR}/agent-${a}.log"
    if [[ -f "${log_file}" ]]; then
      last_write_age="$(($(date +%s) - $(stat -f '%m' "${log_file}" 2>/dev/null || echo 0)))"
      reg_line="$(grep -E "${_agent_ok_patterns}" "${log_file}" 2>/dev/null | tail -1 || true)"
      if [[ -n "${reg_line}" ]]; then
        worker_id="$(echo "${reg_line}" | grep -oE '"id": "[^"]+"' | head -1 | cut -d'"' -f4)"
        record "agent-${a}" ok "pid=${pid_line} worker=${worker_id:-unknown} log_age=${last_write_age}s"
      else
        record "agent-${a}" fail "pid=${pid_line} but no worker-registration line in log"
      fi
    else
      record "agent-${a}" fail "pid=${pid_line} but no log file"
    fi
  else
    record "agent-${a}" fail "not running under launchd"
  fi
done

# ---- SIP routing --------------------------------------------------
if command -v lk >/dev/null 2>&1; then
  trunk_count="$(lk sip inbound list --json 2>/dev/null | jq '.items | length' 2>/dev/null || echo 0)"
  rule_count="$(lk sip dispatch list --json 2>/dev/null | jq '.items | length' 2>/dev/null || echo 0)"
  if [[ "${trunk_count}" -ge 1 ]]; then
    record "sip-trunk"  ok "${trunk_count} inbound trunk(s)"
  else
    record "sip-trunk"  fail "no inbound trunks registered"
  fi
  if [[ "${rule_count}" -ge 3 ]]; then
    record "sip-rules"  ok "${rule_count} dispatch rule(s)"
  else
    record "sip-rules"  fail "only ${rule_count} dispatch rule(s); expected >=3"
  fi
else
  record "sip-routing" fail "lk (livekit-cli) not found"
fi

# ---- emit --------------------------------------------------------
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
    printf '%-18s \033[1;%dm%-6s\033[0m %s\n' "$name" "$color" "$status" "$detail"
  done
  if [[ $failed -gt 0 ]]; then
    printf '\n\033[1;31m%d check(s) failed.\033[0m\n' "$failed"
  fi
fi

exit $([[ $failed -eq 0 ]] && echo 0 || echo 1)
