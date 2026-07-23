#!/usr/bin/env bash
# No-container fault-injection self-test for migrate-stream-slice1.sh.
#
# Proves the RUNBOOK's control flow — trap, guard-death detection, ROLLBACK_OK,
# ROLLBACK_FAILED — with ZERO container/process mutation. The real main()/
# rollback()/guard_ok()/wait_health()/on_err() run unchanged; only the leaf I/O
# (docker/curl/ss/nvidia-smi/pgrep/kill/sleep/stat) and process-spawning
# (arm_watcher) are shadowed by mocks driven from mock-state variables.
#
# META-RED-PROOFS: a copy of the runbook with the trap removed, or a rollback
# success predicate deleted, is re-run through the SAME scenarios; the harness
# asserts those mutants are DETECTED (the scenario no longer meets its
# expectation). If losing a trap/predicate did NOT change the outcome, the
# self-test would be blind — so the meta-checks turn RED on a blind test.
set -uo pipefail
cd "$(dirname "$0")/.."
RUNBOOK=scripts/migrate-stream-slice1.sh
TMP=$(mktemp -d)
trap 'rm -rf "$TMP"' EXIT
PASS=0; FAIL=0

# ---- run one scenario against a runbook file; echo output, return main's exit
run_scenario() { # $1 runbook  $2 setup-fn-name
  local rb="$1" setup="$2"
  (
    # temp, host-safe paths
    local LOGDIR="$TMP/l.$RANDOM"; mkdir -p "$LOGDIR"
    local WF="$LOGDIR/watcher"; printf '#!/bin/sh\n' >"$WF"; chmod +x "$WF"
    # ---- mock state: healthy baseline (a scenario fn tweaks these) ----
    IMGX=sha256:3b28aa8102d69b3214687a7e732dcdeca35b8a11ab0d34187e1dad3f9b4472f7
    m_qual_img="$IMGX"; m_qual_running=true; m_qual_health=200; m_qual_exists=1
    m_tts_exists=1; m_tts_running=false
    m_stable_exists=0; m_stable_img=""; m_stable_health=healthy; m_stable_onnet=1
    m_5056=000; m_dns=OK; m_parakeet=200; m_vram=8000; m_nvidia_rc=0
    m_5056_listener=0; m_old_watcher_n=1; m_diag_present=1
    m_render=200; m_wav_bytes=40000; m_down_rc=0; m_start_rc=0; m_guard_samples=1
    m_qual_never_ready=0; m_running_names="voicebook-stream-qual"
    m_dead_pids=" "; m_guard_budget=""   # pids alive by default; kill adds to dead set
    "$setup"
    source "$rb"                       # defines funcs; BASH_SOURCE guard => no auto-run
    # redirect the runbook's real paths to temp
    LOG="$LOGDIR/migrate.log"; QUAL_LOG="$LOGDIR/qual.log"; W="$WF"
    COMPOSE="$LOGDIR/compose.yaml"; : >"$COMPOSE"
    FAILPOINTS="${SC_FAILPOINTS:-}"

    # ---- mocks (defined AFTER source so they win) ----
    arm_watcher() { local p; case "$1" in voicebook-stream) p=7001;; *) p=7002;; esac
      [ "$m_guard_samples" = 1 ] && echo SAMPLE >>"$2"; echo "$p"; }  # pid alive by default (dead-set model)
    docker() { local s="$1"; shift; case "$s" in
        image) return "$([ "$1" = inspect ] && shift; { [ "$1" = "$IMGX" ] && echo 0; } || { [ "$1" = alpine ] && [ "$m_diag_present" = 1 ] && echo 0; } || echo 1)";;
        volume|network) return 0;;
        ps) printf '%s\n' $m_running_names;;
        stop) m_qual_running=false; m_qual_health=000; return 0;;
        start) [ "$m_qual_never_ready" = 1 ] || { m_qual_running=true; m_qual_health=200; }; return "$m_start_rc";;
        run) echo "$m_dns";;
        compose) case "$*" in
            *down*) m_stable_exists=0; m_5056=000; m_running_names="voicebook-stream-qual"; return "$m_down_rc";;
            *"up -d"*) m_stable_exists=1; m_stable_img="$IMGX"; m_stable_health=healthy; m_5056=200; m_dns=OK; m_stable_onnet=1; m_running_names="voicebook-stream"; return 0;;
          esac;;
        inspect) local fmt="" name=""
          while [ $# -gt 0 ]; do case "$1" in -f|--format) fmt="$2"; shift 2;; *) name="$1"; shift;; esac; done
          case "$name" in
            voicebook-stream-qual) case "$fmt" in
                *Image*) echo "$m_qual_img";; *State.Running*) echo "$m_qual_running";;
                *State.Status*) [ "$m_qual_running" = true ] && echo running || echo exited;;
                "") [ "$m_qual_exists" = 1 ] && return 0 || return 1;; esac;;
            voicebook-tts) case "$fmt" in
                *State.Running*) if [ "$PHASE" = preflight ]; then echo false; else echo "$m_tts_running"; fi;;
                *State.Status*) echo exited;;
                "") if [ "$PHASE" = preflight ]; then return 0; else [ "$m_tts_exists" = 1 ] && return 0 || return 1; fi;; esac;;
            voicebook-stream) case "$fmt" in
                *Image*) echo "$m_stable_img";; *Health.Status*) echo "$m_stable_health";;
                *Networks*) [ "$m_stable_onnet" = 1 ] && echo "voice_default " || echo "";;
                "") [ "$m_stable_exists" = 1 ] && return 0 || return 1;; esac;;
          esac;;
      esac; }
    curl() { local o="" url=""
      while [ $# -gt 0 ]; do case "$1" in -o) o="$2"; shift 2;; -X|-H|-d|-m|-w) shift 2;; -*) shift;; *) url="$1"; shift;; esac; done
      local code=000
      case "$url" in *5060/healthz) code=$m_qual_health;; *5056/healthz) code=$m_5056;;
        *5056/speak) code=$m_render;; *health/ready) code=$m_parakeet;; esac
      [ -n "$o" ] && : >"$o" 2>/dev/null; printf '%s' "$code"; }
    nvidia-smi() { echo "$m_vram"; return "$m_nvidia_rc"; }
    ss() { echo "hdr"; [ "$m_5056_listener" = 1 ] && echo "LISTEN 0 0 0.0.0.0:5056"; }
    pgrep() { case "$1" in -fc) echo "$m_old_watcher_n";; -f) local i; for i in $(seq 1 "$m_old_watcher_n"); do echo "$((9000+i))"; done;; esac; }
    kill() { if [ "$1" = -0 ]; then
        if [ "$2" = 7001 ] && [ -n "$m_guard_budget" ]; then
          [ "$m_guard_budget" -le 0 ] && return 1; m_guard_budget=$((m_guard_budget-1)); return 0; fi
        case " $m_dead_pids " in *" $2 "*) return 1;; *) return 0;; esac
      else local p; for p in "$@"; do case "$p" in -*) continue;; esac; m_dead_pids="$m_dead_pids$p "; done; return 0; fi; }
    sleep() { :; }
    stat() { echo "$m_wav_bytes"; }
    bash() { case "$*" in *test-stream-compose.sh*) echo "== SLICE1_TEST=PASS ==";; *) command bash "$@";; esac; }

    main
  ) 2>&1
}

# ---- expectation helpers -------------------------------------------------
expect() { # label runbook setup want_exit want_token [reason_substr]
  local label="$1" out rc
  out=$(run_scenario "$2" "$3"); rc=$?
  if [ "$rc" = "$4" ] && grep -q "MIGRATE_RESULT=$5" <<<"$out" && { [ -z "${6:-}" ] || grep -qi -- "$6" <<<"$out"; }; then
    echo "PASS  $label  (exit=$rc, $5${6:+, reason~$6})"; PASS=$((PASS+1))
  else
    echo "FAIL  $label  (got exit=$rc want $4; want token $5${6:+, reason $6})"; sed 's/^/        | /' <<<"$out"; FAIL=$((FAIL+1))
  fi
}
meta_detects() { # label mutant setup want_exit want_token  (asserts mutant FAILS the expectation)
  local label="$1" out rc
  out=$(run_scenario "$2" "$3"); rc=$?
  if [ "$rc" = "$4" ] && grep -q "MIGRATE_RESULT=$5" <<<"$out"; then
    echo "META-FAIL  $label  — mutant still met expectation; self-test is BLIND to this regression"; FAIL=$((FAIL+1))
  else
    echo "PASS  $label  — regression detected (real scenario would turn RED; mutant got exit=$rc)"; PASS=$((PASS+1))
  fi
}
mutant() { sed "$1" "$RUNBOOK" >"$TMP/mut.sh"; echo "$TMP/mut.sh"; }

# ---- scenarios -----------------------------------------------------------
s_happy()      { :; }
s_trap()       { m_nvidia_rc=1; }                       # unexpected failure -> ERR trap (not a || rollback)
s_guard_death(){ m_guard_budget=2; }                    # MIG guard dies at the after-startup checkpoint
s_failpoint()  { SC_FAILPOINTS="before_render"; }       # explicit allowlisted failpoint, restore healthy
s_fail_ds()    { SC_FAILPOINTS="before_render"; m_start_rc=1; }   # rollback: docker start rc!=0 (seam 1)
s_fail_dc()    { SC_FAILPOINTS="before_render"; m_down_rc=1; }    # rollback: compose down rc!=0 (seam 1)
s_fail_notready(){ SC_FAILPOINTS="before_render"; m_qual_never_ready=1; }  # rollback: qual never 5060-ready
s_fail_tts()   { SC_FAILPOINTS="before_render"; m_tts_running=true; }      # rollback: tts running (seam 2)
s_fail_qualwatch(){ m_guard_samples=0; }                # qual watcher won't sample => rollback gstat BAD (seam 3)
s_preflight()  { m_qual_img="sha256:DEADBEEF"; }        # preflight abort, NO mutation, NO rollback

echo "=========================================================="
echo " MOCK FAULT-INJECTION SELF-TEST — migrate-stream-slice1.sh"
echo "=========================================================="
echo "--- primary control-flow scenarios (real runbook) ---"
expect "SUCCESS/happy-path"            "$RUNBOOK" s_happy          0 SUCCESS
expect "TRAP->ROLLBACK_OK"             "$RUNBOOK" s_trap           1 ROLLBACK_OK   "trap"
expect "GUARD-DEATH->ROLLBACK_OK"      "$RUNBOOK" s_guard_death    1 ROLLBACK_OK   "guard"
expect "FAILPOINT->ROLLBACK_OK"        "$RUNBOOK" s_failpoint      1 ROLLBACK_OK   "before_render"
expect "ROLLBACK_FAILED/start-rc(#1)"  "$RUNBOOK" s_fail_ds        2 ROLLBACK_FAILED
expect "ROLLBACK_FAILED/down-rc(#1)"   "$RUNBOOK" s_fail_dc        2 ROLLBACK_FAILED
expect "ROLLBACK_FAILED/qual-not-ready" "$RUNBOOK" s_fail_notready 2 ROLLBACK_FAILED
expect "ROLLBACK_FAILED/tts-running(#2)" "$RUNBOOK" s_fail_tts     2 ROLLBACK_FAILED
expect "ROLLBACK_FAILED/qual-watcher(#3)" "$RUNBOOK" s_fail_qualwatch 2 ROLLBACK_FAILED
expect "PREFLIGHT_ABORT/no-mutation"   "$RUNBOOK" s_preflight      1 PREFLIGHT_ABORT

echo "--- meta-red-proofs (mutated runbook MUST be detected) ---"
M_NOTRAP=$(mutant '/^  trap on_err ERR HUP INT TERM$/d')      # lose the phase-aware trap
meta_detects "lost-trap detected"        "$M_NOTRAP" s_trap        1 ROLLBACK_OK
M_NODS=$(mutant 's/ \[ "\$ds" = 0 \] &&//')                    # lose the start-rc success predicate
meta_detects "lost-ds-predicate detected" "$M_NODS" s_fail_ds      2 ROLLBACK_FAILED
M_NODC=$(mutant 's/\[ "\$dc" = 0 \] && //')                    # lose the down-rc success predicate
meta_detects "lost-dc-predicate detected" "$M_NODC" s_fail_dc      2 ROLLBACK_FAILED
M_NOTTS=$(mutant 's/ \[ "\$ttsr" = false \] &&//')             # lose the tts-stopped success predicate
meta_detects "lost-tts-predicate detected" "$M_NOTTS" s_fail_tts   2 ROLLBACK_FAILED
M_NOGSTAT=$(mutant 's/ && \[ "\$gstat" = OK \]//')             # lose the re-armed-qual-watcher predicate (final conjunct)
meta_detects "lost-qual-watcher-predicate detected" "$M_NOGSTAT" s_fail_qualwatch 2 ROLLBACK_FAILED

echo "=========================================================="
echo "PASS=$PASS FAIL=$FAIL"
[ "$FAIL" = 0 ] && echo "== SELFTEST=PASS ==" || echo "== SELFTEST=FAIL =="
[ "$FAIL" = 0 ]
