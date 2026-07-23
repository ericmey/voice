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
SCF="$TMP/start.calls"   # docker-start-qual call log; one line per real start (mock)

# ---- run one scenario against a runbook file; echo output, return main's exit
run_scenario() { # $1 runbook  $2 setup-fn-name
  local rb="$1" setup="$2"
  (
    # temp, host-safe paths
    local LOGDIR="$TMP/l.$RANDOM"; mkdir -p "$LOGDIR"; : > "$SCF"   # fresh start-call log per scenario
    local WF="$LOGDIR/watcher"; printf '#!/bin/sh\n' >"$WF"; chmod +x "$WF"
    # ---- mock state: healthy baseline (a scenario fn tweaks these) ----
    IMGX=sha256:3b28aa8102d69b3214687a7e732dcdeca35b8a11ab0d34187e1dad3f9b4472f7
    m_qual_img="$IMGX"; m_qual_running=true; m_qual_health=200; m_qual_exists=1
    m_tts_exists=1; m_tts_running=false
    m_stable_exists=0; m_stable_running=false; m_stable_img=""; m_stable_health=healthy; m_stable_onnet=1; m_stable_project=""; m_bad_project=0
    m_5056=000; m_dns=OK; m_parakeet=200; m_vram=8000; m_nvidia_rc=0
    m_5056_listener=0; m_old_watcher_n=1; m_diag_present=1; m_composetest_rc=0
    m_render=200; m_wav_bytes=40000; m_down_rc=0; m_start_rc=0; m_guard_samples=1
    m_qual_never_ready=0; m_running_names="voicebook-stream-qual"; m_ps_rc=0
    m_dead_pids=" "; m_unkillable=" "; m_guard_budget=""   # pids alive by default; kill adds to dead set
    "$setup"
    source "$rb"                       # defines funcs; BASH_SOURCE guard => no auto-run
    # redirect the runbook's real paths to temp
    LOG="$LOGDIR/migrate.log"; QUAL_LOG="$LOGDIR/qual.log"; W="$WF"
    COMPOSE="$LOGDIR/compose.yaml"; : >"$COMPOSE"
    # MODE flows in from the scenario (set before source); the runbook derives + validates FAILPOINTS

    # ---- mocks (defined AFTER source so they win) ----
    arm_watcher() { local p; case "$1" in voicebook-stream) p=7001;; *) p=7002;; esac
      [ "$m_guard_samples" = 1 ] && echo SAMPLE >>"$2"; echo "$p"; }  # pid alive by default (dead-set model)
    docker() { local s="$1"; shift; case "$s" in
        image) return "$([ "$1" = inspect ] && shift; { [ "$1" = "$IMGX" ] && echo 0; } || { [ "$1" = alpine ] && [ "$m_diag_present" = 1 ] && echo 0; } || echo 1)";;
        volume|network) return 0;;
        ps) case "$*" in
            # exact-name inventory readback: rc distinguishes UNKNOWN from a real answer
            *-a*) [ "$m_ps_rc" != 0 ] && return "$m_ps_rc"
                  if [ "$m_stable_exists" = 1 ]; then
                    case "$m_stable_running" in true) echo running;; false) echo exited;; *) echo "$m_stable_running";; esac
                  fi; return 0;;
            *) printf '%s\n' $m_running_names;;   # plain ps --format Names (wait_health)
          esac;;
        stop) m_qual_running=false; m_qual_health=000; return 0;;
        start) echo "START $*" >> "$SCF"; [ "$m_qual_never_ready" = 1 ] || { m_qual_running=true; m_qual_health=200; }; return "$m_start_rc";;
        run) echo "$m_dns";;
        compose) case "$*" in
            # a FAILED down (rc!=0) leaves the stable RUNNING -> exercises the no-two-model guard
            *down*) if [ "$m_down_rc" = 0 ]; then m_stable_exists=0; m_stable_running=false; m_5056=000; m_running_names="voicebook-stream-qual"; fi; return "$m_down_rc";;
            *"up -d"*) m_stable_exists=1; m_stable_running=true; m_stable_img="$IMGX"; m_stable_health=healthy; m_5056=200; m_dns=OK; m_stable_onnet=1; m_running_names="voicebook-stream"
                       [ "$m_bad_project" = 1 ] && m_stable_project=vbs-drill-a6a9c4e || m_stable_project=voicebook-stream; return 0;;
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
                *compose.project*) echo "$m_stable_project";;
                *Image*) echo "$m_stable_img";; *Health.Status*) echo "$m_stable_health";;
                *State.Running*) echo "$m_stable_running";;
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
      else local p; for p in "$@"; do case "$p" in -*) continue;; esac
          case " $m_unkillable " in *" $p "*) continue;; esac   # kill-resistant: ignores every signal
          m_dead_pids="$m_dead_pids$p "; done; return 0; fi; }
    sleep() { :; }
    stat() { echo "$m_wav_bytes"; }
    bash() { case "$*" in *test-stream-compose.sh*) echo "== SLICE1_TEST=PASS =="; return "$m_composetest_rc";; *) command bash "$@";; esac; }

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
meta_line_gone() { # label mutant setup substr  (real HAS the safeguard line; mutant must NOT)
  local label="$1" out
  out=$(run_scenario "$2" "$3")
  if grep -q -- "$4" <<<"$out"; then
    echo "META-FAIL  $label  — safeguard '$4' still fires in mutant; self-test is BLIND"; FAIL=$((FAIL+1))
  else
    echo "PASS  $label  — safeguard '$4' removed => regression detected"; PASS=$((PASS+1))
  fi
}
expect_no_start() { # label runbook setup — ROLLBACK_FAILED + abort line + ZERO real docker-start-qual calls
  local label="$1" out rc n
  out=$(run_scenario "$2" "$3"); rc=$?; n=$(wc -l <"$SCF" | tr -d ' ')
  if [ "$rc" = 2 ] && grep -q 'ROLLBACK_ABORT_NO_START' <<<"$out" && [ "$n" = 0 ]; then
    echo "PASS  $label  (exit=2, abort line, docker-start-qual calls=$n)"; PASS=$((PASS+1))
  else
    echo "FAIL  $label  (exit=$rc, abort-line=$(grep -qc ROLLBACK_ABORT_NO_START <<<"$out" || echo 0), start-calls=$n)"; sed 's/^/        | /' <<<"$out"; FAIL=$((FAIL+1))
  fi
}
meta_start_called() { # label mutant setup — mutant MUST actually call docker start qual (count>0): a real two-model regression
  local label="$1" n
  run_scenario "$2" "$3" >/dev/null; n=$(wc -l <"$SCF" | tr -d ' ')
  if [ "$n" -gt 0 ]; then
    echo "PASS  $label  — mutant called docker start qual ($n) with stable unsafe => two-model regression detected"; PASS=$((PASS+1))
  else
    echo "META-FAIL  $label  — mutant did NOT call start; self-test is BLIND to the two-model regression"; FAIL=$((FAIL+1))
  fi
}
mutant() { sed "$1" "$RUNBOOK" >"$TMP/mut.sh"; echo "$TMP/mut.sh"; }

# ---- scenarios -----------------------------------------------------------
s_happy()        { MODE=clean; }
s_trap()         { MODE=clean; m_nvidia_rc=1; }         # unexpected failure -> ERR trap (not a || rollback)
s_guard_death()  { MODE=clean; m_guard_budget=2; }      # MIG guard dies at the after-startup checkpoint
s_failpoint()    { MODE=drill; }                        # before_render failpoint, restore healthy
s_fail_ds()      { MODE=drill; m_start_rc=1; }          # down ok(stable gone)->start fails: ds!=0 predicate
s_two_model()    { MODE=drill; m_down_rc=1; }           # down fails->stable running->NO_START abort (BarB #1)
s_readback_error(){ MODE=drill; m_down_rc=1; m_ps_rc=1; }  # FIRST readback ERRORS while stable actually running -> UNKNOWN -> fail-closed (BarB #1 final)
s_fail_notready(){ MODE=drill; m_qual_never_ready=1; }  # rollback: qual never 5060-ready
s_fail_tts()     { MODE=drill; m_tts_running=true; }    # rollback: tts running (v3 seam 2)
s_fail_qualwatch(){ MODE=clean; m_guard_samples=0; }    # qual watcher won't sample => gstat BAD (v3 seam 3)
s_unkill_rb()    { MODE=drill; m_unkillable=" 7001 "; }  # rollback: mig watcher won't die -> mwdead!=0 (BarB #2)
s_unkill_handoff(){ MODE=clean; m_unkillable=" 7001 "; } # SUCCESS path: watcher won't die -> HANDOFF_WATCHER_ALIVE
s_project_wrong(){ MODE=clean; m_bad_project=1; }      # stable comes up with staging-dir project label -> rollback (BarB lifecycle)
s_preflight()    { MODE=clean; m_qual_img="sha256:DEADBEEF"; }  # preflight abort, NO mutation
s_mode_typo()    { MODE=driIl; }                        # invalid MODE -> preflight abort, NO mutation (BarB #3)
s_composetest()  { MODE=clean; m_composetest_rc=1; }    # structured test rc!=0 -> preflight abort (tightening)

echo "=========================================================="
echo " MOCK FAULT-INJECTION SELF-TEST — migrate-stream-slice1.sh"
echo "=========================================================="
echo "--- primary control-flow scenarios (real runbook) ---"
expect "SUCCESS/happy-path"              "$RUNBOOK" s_happy          0 SUCCESS
expect "TRAP->ROLLBACK_OK"               "$RUNBOOK" s_trap           1 ROLLBACK_OK   "trap"
expect "GUARD-DEATH->ROLLBACK_OK"        "$RUNBOOK" s_guard_death    1 ROLLBACK_OK   "guard"
expect "FAILPOINT->ROLLBACK_OK"          "$RUNBOOK" s_failpoint      1 ROLLBACK_OK   "before_render"
expect "ROLLBACK_FAILED/start-rc"        "$RUNBOOK" s_fail_ds        2 ROLLBACK_FAILED
expect_no_start "NO-TWO-MODEL/running(B#1)"  "$RUNBOOK" s_two_model
expect_no_start "NO-TWO-MODEL/readback-error-fail-closed(B#1)" "$RUNBOOK" s_readback_error
expect "ROLLBACK_FAILED/qual-not-ready"  "$RUNBOOK" s_fail_notready  2 ROLLBACK_FAILED
expect "ROLLBACK_FAILED/tts-running"     "$RUNBOOK" s_fail_tts       2 ROLLBACK_FAILED
expect "ROLLBACK_FAILED/qual-watcher"    "$RUNBOOK" s_fail_qualwatch 2 ROLLBACK_FAILED
expect "ROLLBACK_FAILED/watcher-unkillable(B#2)" "$RUNBOOK" s_unkill_rb 2 ROLLBACK_FAILED "NOT confirmed dead"
expect "HANDOFF-RECOVERS-TO-QUAL(B#3)"   "$RUNBOOK" s_unkill_handoff 2 ROLLBACK_FAILED "recovering to qual"
expect "ROLLBACK_OK/wrong-project-label" "$RUNBOOK" s_project_wrong  1 ROLLBACK_OK "project label"
expect "PREFLIGHT_ABORT/digest"          "$RUNBOOK" s_preflight      1 PREFLIGHT_ABORT
expect "PREFLIGHT_ABORT/mode-typo(B#3)"  "$RUNBOOK" s_mode_typo      1 PREFLIGHT_ABORT  "MODE must be"
expect "PREFLIGHT_ABORT/composetest-rc"  "$RUNBOOK" s_composetest    1 PREFLIGHT_ABORT  "compose test failed"

echo "--- meta-red-proofs (mutated runbook MUST be detected) ---"
M_NOTRAP=$(mutant '/^  trap on_err ERR HUP INT TERM$/d')      # lose the phase-aware trap
meta_detects "lost-trap detected"        "$M_NOTRAP" s_trap        1 ROLLBACK_OK
M_NODS=$(mutant 's/ \[ "\$ds" = 0 \] &&//')                    # lose the start-rc success predicate
meta_detects "lost-ds-predicate detected" "$M_NODS" s_fail_ds      2 ROLLBACK_FAILED
M_NOTTS=$(mutant 's/ \[ "\$ttsr" = false \] &&//')             # lose the tts-stopped success predicate
meta_detects "lost-tts-predicate detected" "$M_NOTTS" s_fail_tts   2 ROLLBACK_FAILED
M_NOGSTAT=$(mutant 's/ && \[ "\$gstat" = OK \]//')             # lose the re-armed-qual-watcher predicate (final conjunct)
meta_detects "lost-qual-watcher-predicate detected" "$M_NOGSTAT" s_fail_qualwatch 2 ROLLBACK_FAILED
M_NOMW=$(mutant 's/\[ "\$mwdead" = 0 \] && //')                # BarB #2: lose the migration-watcher-dead predicate
meta_detects "lost-mwdead-predicate detected" "$M_NOMW" s_unkill_rb 2 ROLLBACK_FAILED
M_NOHANDOFF=$(mutant '/rollback "handoff: migration watcher/d')  # BarB #3: lose the handoff->qual recovery
meta_detects "lost-handoff-recovery detected" "$M_NOHANDOFF" s_unkill_handoff 2 ROLLBACK_FAILED
M_NO2MODEL=$(mutant 's/\[ "\$stable_gone" != 1 \]/false/')     # BarB #1: neuter the no-two-model guard
meta_start_called "no-two-model-guard: mutant actually starts qual" "$M_NO2MODEL" s_two_model
M_FAILOPEN=$(mutant 's/\[ "\$rrc" != 0 \]; then stable_gone=0/[ "$rrc" != 0 ]; then stable_gone=1/')  # BarB #1: infer ABSENCE from a failed readback
meta_start_called "never-infer-absence: mutant starts qual on readback error" "$M_FAILOPEN" s_readback_error
M_NOPROJECT=$(mutant '/com.docker.compose.project/d')          # BarB lifecycle: lose ALL project-label guards
meta_detects "lost-project-label-guard detected" "$M_NOPROJECT" s_project_wrong 1 ROLLBACK_OK

echo "=========================================================="
echo "PASS=$PASS FAIL=$FAIL"
[ "$FAIL" = 0 ] && echo "== SELFTEST=PASS ==" || echo "== SELFTEST=FAIL =="
[ "$FAIL" = 0 ]
