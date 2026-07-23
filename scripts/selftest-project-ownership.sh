#!/usr/bin/env bash
# No-container fault-injection self-test for migrate-project-ownership.sh.
# Drives the REAL preflight/migrate/verify/rollback control flow with ZERO
# container mutation; only leaf I/O (docker/curl/realpath/shasum/stat/sleep) is
# shadowed. Meta-red-proofs (Yua's list): stale hash, lost running check, unknown
# state readback, running/unhealthy stable before qual fallback, and each
# incomplete ROLLBACK_OK tier.
set -uo pipefail
cd "$(dirname "$0")/.."
RUNBOOK=scripts/migrate-project-ownership.sh
TMP=$(mktemp -d); trap 'rm -rf "$TMP"' EXIT
PASS=0; FAIL=0
SCF="$TMP/qualstart.calls"

run_scenario() { # $1 runbook  $2 setup-fn
  local rb="$1" setup="$2"
  (
    local LOGDIR="$TMP/l.$RANDOM"; mkdir -p "$LOGDIR"; : > "$SCF"
    printf 'compose\n' > "$LOGDIR/docker-compose.stream.yaml"     # real file so [ -f ] passes; shasum is mocked
    IMGX=sha256:3b28aa8102d69b3214687a7e732dcdeca35b8a11ab0d34187e1dad3f9b4472f7
    SHAX=cb3dc23449aafebbc5e4c4d2d3c16c1adc8d2cba2bec4e781b2c0f1fc12f3899
    # ---- mock state: healthy OLD service baseline ----
    m_stable_present=1; m_stable_running=true; m_stable_health=healthy; m_stable_img="$IMGX"
    m_stable_project=vbs-drill-a6a9c4e; m_5056=200; m_dns=OK
    m_qual_status=exited; m_qual_img="$IMGX"; m_qual_running=false; m_qual_5060=000
    m_tts_running=false; m_parakeet_ready=200; m_parakeet_live=200; m_vram=8000; m_sha="$SHAX"; m_render_name=voicebook-stream
    m_old_ps="voicebook-stream"; m_canon_ps=""; m_ps_rc=0; m_render=200; m_bytes=40000
    m_up_n=0; m_up1_rc=0; m_up2_rc=0; m_up_health_rb=healthy; m_up_img_rb="$IMGX"
    m_canon_abs="$LOGDIR"; m_staging_abs="/no/vbs-drill-a6a9c4e"; m_canon_home="$LOGDIR"
    "$setup"
    # simulate launching WITH an EXPECT_CANON_DIR in the environment (must be ignored by the literal pin)
    [ "${m_skip_pin_override:-0}" = 1 ] && EXPECT_CANON_DIR="${m_env_pin:-/x}"
    source "$rb"
    CANON_DIR="$LOGDIR"; STAGING_DIR="/no/vbs-drill-a6a9c4e"
    PARAKEET_READY=http://127.0.0.1:9000/v1/health/ready; PARAKEET_LIVE=http://127.0.0.1:9000/v1/health/live; VRAM_FLOOR=800
    # normal scenarios override the pin AFTER source (Yua-sanctioned test seam); the env-red-proof does NOT
    [ "${m_skip_pin_override:-0}" = 1 ] || EXPECT_CANON_DIR="$m_canon_home"

    realpath() { case "$1" in *vbs-drill*) echo "$m_staging_abs";; *MISMATCH*) echo /different/home;; *voicebook-stream-deploy*) echo /home/ericmey/voicebook-stream-deploy;; *) echo "$m_canon_abs";; esac; }
    shasum() { echo "$m_sha  ${!#}"; }
    stat() { echo "$m_bytes"; }
    sleep() { :; }
    nvidia-smi() { echo "$m_vram"; }
    docker() { local s="$1"; shift; case "$s" in
        inspect) local fmt="" name=""
          while [ $# -gt 0 ]; do case "$1" in -f|--format) fmt="$2"; shift 2;; *) name="$1"; shift;; esac; done
          case "$name" in
            voicebook-stream) case "$fmt" in
                *compose.project*) echo "$m_stable_project";; *Image*) echo "$m_stable_img";;
                *Health.Status*) echo "$m_stable_health";; *State.Running*) echo "$m_stable_running";;
                *State.Status*) [ "$m_stable_running" = true ] && echo running || echo exited;;
                "") [ "$m_stable_present" = 1 ] && return 0 || return 1;; esac;;
            voicebook-stream-qual) case "$fmt" in
                *Image*) echo "$m_qual_img";; *State.Running*) echo "$m_qual_running";;
                *State.Status*) echo "$m_qual_status";; "") return 0;; esac;;
            voicebook-tts) case "$fmt" in *State.Running*) echo "$m_tts_running";; "") return 0;; esac;;
          esac;;
        ps) case " $* " in
            *" -a "*) [ "$m_ps_rc" != 0 ] && return "$m_ps_rc"; [ "$m_stable_present" = 1 ] && { [ "$m_stable_running" = true ] && echo running || echo exited; }; return 0;;
            *) [ "$m_stable_present" = 1 ] && echo voicebook-stream;; esac;;
        run) echo "$m_dns";;
        start) echo START >> "$SCF"; case "$1" in voicebook-stream-qual) m_qual_running=true; m_qual_status=running; m_qual_5060=200;; esac; return 0;;
        compose) case " $* " in
            *" config "*) echo "{\"name\":\"$m_render_name\"}";;
            *" ps "*) case " $* " in *" -p "*) echo "$m_old_ps";; *) echo "$m_canon_ps";; esac;;
            *" down "*) m_stable_present=0; m_stable_running=false; m_5056=000; m_dns=NO; return 0;;
            *" up "*) m_up_n=$((m_up_n+1)); local rc; [ "$m_up_n" = 1 ] && rc=$m_up1_rc || rc=$m_up2_rc
                      if [ "$rc" = 0 ]; then m_stable_present=1; m_stable_running=true; m_stable_project=voicebook-stream; m_5056=200; m_dns=OK
                        if [ "$m_up_n" -ge 2 ]; then m_stable_health=$m_up_health_rb; m_stable_img=$m_up_img_rb; else m_stable_health=healthy; m_stable_img=$IMGX; fi
                      fi; return "$rc";;
          esac;;
      esac; }
    curl() { local o="" url=""
      while [ $# -gt 0 ]; do case "$1" in -o) o="$2"; shift 2;; -X|-H|-d|-m|-w) shift 2;; -*) shift;; *) url="$1"; shift;; esac; done
      local c=000; case "$url" in *5056/healthz) c=$m_5056;; *5060/healthz) c=$m_qual_5060;; *5056/speak) c=$m_render;; *health/ready) c=$m_parakeet_ready;; *health/live) c=$m_parakeet_live;; esac
      [ -n "$o" ] && : > "$o" 2>/dev/null; printf '%s' "$c"; }

    main
  ) 2>&1
}

expect() { local label="$1" out rc; out=$(run_scenario "$2" "$3"); rc=$?
  if [ "$rc" = "$4" ] && grep -q "OWNERSHIP_RESULT=$5" <<<"$out" && { [ -z "${6:-}" ] || grep -qi -- "$6" <<<"$out"; }; then
    echo "PASS  $label  (exit=$rc, $5${6:+, ~$6})"; PASS=$((PASS+1))
  else echo "FAIL  $label  (exit=$rc want $4; token $5${6:+ ~$6})"; sed 's/^/        | /' <<<"$out"; FAIL=$((FAIL+1)); fi; }
expect_no_qual_start() { local label="$1" out rc n; out=$(run_scenario "$2" "$3"); rc=$?; n=$(wc -l <"$SCF" | tr -d ' ')
  if [ "$rc" = 2 ] && grep -q 'OWNERSHIP_RESULT=ROLLBACK_FAILED' <<<"$out" && [ "$n" = 0 ]; then
    echo "PASS  $label  (exit=2, ROLLBACK_FAILED, qual-start calls=$n)"; PASS=$((PASS+1))
  else echo "FAIL  $label  (exit=$rc, qual-start=$n)"; sed 's/^/        | /' <<<"$out"; FAIL=$((FAIL+1)); fi; }
meta_detects() { local label="$1" out rc; out=$(run_scenario "$2" "$3"); rc=$?
  if [ "$rc" = "$4" ] && grep -q "OWNERSHIP_RESULT=$5" <<<"$out"; then
    echo "META-FAIL  $label — mutant still met expectation; BLIND"; FAIL=$((FAIL+1))
  else echo "PASS  $label — regression detected (mutant exit=$rc)"; PASS=$((PASS+1)); fi; }
meta_qual_started() { local label="$1" n; run_scenario "$2" "$3" >/dev/null; n=$(wc -l <"$SCF" | tr -d ' ')
  if [ "$n" -gt 0 ]; then echo "PASS  $label — mutant started qual ($n) beside running/unknown stable => detected"; PASS=$((PASS+1))
  else echo "META-FAIL  $label — mutant did NOT start qual; BLIND"; FAIL=$((FAIL+1)); fi; }
mutant() { sed "$1" "$RUNBOOK" > "$TMP/mut.sh"; echo "$TMP/mut.sh"; }

# ---- scenarios ----
s_success()      { :; }
s_stalehash()    { m_sha=sha256:STALE; }
s_notrunning()   { m_stable_running=false; }                    # stable present but not running
s_state_a()      { m_render=500; }                              # migrate ok, render fails -> rollback re-ups canonical healthy
s_state_b()      { m_old_ps="voicebook-stream-qual"; m_up1_rc=1; }  # exact-target mismatch pre-down; rollback up fails; old healthy
s_state_c()      { m_up1_rc=1; m_up2_rc=1; }                    # migrate up fails after down; rollback up fails; stable gone -> qual
s_fail_running() { m_render=500; m_up_health_rb=starting; }     # rollback leaves stable running+unhealthy -> refuse qual
s_fail_unknown() { m_up1_rc=1; m_up2_rc=1; m_ps_rc=1; }         # stable gone but readback UNKNOWN -> refuse qual
s_wrongdigest()  { m_render=500; m_up_img_rb=sha256:BADD; }     # rollback stable healthy+canonical but WRONG digest -> not A
s_par_ready()    { m_parakeet_ready=500; }                      # GATE1: ready fails independently of live
s_par_live()     { m_parakeet_live=500; }                       # GATE1: live fails independently of ready
s_vram_empty()   { m_vram=""; }                                 # GATE2: empty readback -> fail-closed
s_vram_nonnum()  { m_vram=oops; }                               # GATE2: non-numeric -> fail-closed
s_vram_below()   { m_vram=500; }                                # GATE2: below floor -> fail-closed
s_home_mismatch(){ m_canon_home=/x/MISMATCH/home; }             # CANON pin: arg resolves != EXPECT_CANON_DIR
s_env_pin()      { m_skip_pin_override=1; m_env_pin="$m_canon_abs"; }  # env EXPECT_CANON_DIR set to CANON_DIR's path; literal pin must IGNORE it

echo "=============================================================="
echo " OWNERSHIP-MIGRATION SELF-TEST — migrate-project-ownership.sh"
echo "=============================================================="
expect "SUCCESS/happy-reown"           "$RUNBOOK" s_success     0 SUCCESS
expect "PREFLIGHT_ABORT/stale-hash"    "$RUNBOOK" s_stalehash   1 PREFLIGHT_ABORT "hash"
expect "PREFLIGHT_ABORT/not-running"   "$RUNBOOK" s_notrunning  1 PREFLIGHT_ABORT "State.Running"
expect "ROLLBACK_OK/STATE_A-canonical" "$RUNBOOK" s_state_a     1 ROLLBACK_OK "STATE_A"
expect "ROLLBACK_OK/STATE_B-old"       "$RUNBOOK" s_state_b     1 ROLLBACK_OK "STATE_B"
expect "ROLLBACK_OK/STATE_C-qual"      "$RUNBOOK" s_state_c     1 ROLLBACK_OK "STATE_C"
expect_no_qual_start "ROLLBACK_FAILED/stable-running-unhealthy" "$RUNBOOK" s_fail_running
expect_no_qual_start "ROLLBACK_FAILED/readback-unknown"        "$RUNBOOK" s_fail_unknown
expect "ROLLBACK_FAILED/incomplete-A(wrong-digest)" "$RUNBOOK" s_wrongdigest 2 ROLLBACK_FAILED
expect "PREFLIGHT_ABORT/parakeet-ready-only(G1)" "$RUNBOOK" s_par_ready 1 PREFLIGHT_ABORT "Parakeet"
expect "PREFLIGHT_ABORT/parakeet-live-only(G1)"  "$RUNBOOK" s_par_live  1 PREFLIGHT_ABORT "Parakeet"
expect "PREFLIGHT_ABORT/vram-empty(G2)"          "$RUNBOOK" s_vram_empty  1 PREFLIGHT_ABORT "VRAM"
expect "PREFLIGHT_ABORT/vram-nonnumeric(G2)"     "$RUNBOOK" s_vram_nonnum 1 PREFLIGHT_ABORT "VRAM"
expect "PREFLIGHT_ABORT/vram-below-floor(G2)"    "$RUNBOOK" s_vram_below  1 PREFLIGHT_ABORT "VRAM"
expect "PREFLIGHT_ABORT/canon-home-mismatch"     "$RUNBOOK" s_home_mismatch 1 PREFLIGHT_ABORT "EXPECT_CANON_DIR"
expect "PIN-IGNORES-ENV/env-cannot-move-pin"     "$RUNBOOK" s_env_pin       1 PREFLIGHT_ABORT "EXPECT_CANON_DIR"

echo "--- meta-red-proofs ---"
M_NOHASH=$(mutant '/\[ "\$sha" = "\$EXPECT_COMPOSE_SHA" \]/d')
meta_detects "lost-hash-guard detected"        "$M_NOHASH" s_stalehash 1 PREFLIGHT_ABORT
M_NORUN=$(mutant '/\[ "\$(running voicebook-stream)" = true \]/d')
meta_detects "lost-running-check detected"     "$M_NORUN" s_notrunning 1 PREFLIGHT_ABORT
M_NOGATE=$(mutant 's/if \[ "\$g" = yes \] || { \[ "\$g" = no \] && \[ "\$(running voicebook-stream)" = false \]; }; then/if true; then/')
meta_qual_started "no-two-model gate: running stable" "$M_NOGATE" s_fail_running
meta_qual_started "no-two-model gate: unknown readback" "$M_NOGATE" s_fail_unknown
M_NOADIGEST=$(mutant 's/ \[ "\$img" = "\$IMG" \] && \[ "\$lbl" = "\$PROJECT" \]/ [ "$lbl" = "$PROJECT" ]/')
meta_detects "incomplete-STATE_A (drop digest) detected" "$M_NOADIGEST" s_wrongdigest 2 ROLLBACK_FAILED
M_NOREADY=$(mutant 's/\[ "\$(hc "\$PARAKEET_READY")" = 200 \] && //')       # GATE1: drop ready check
meta_detects "lost-parakeet-ready detected"    "$M_NOREADY" s_par_ready 1 PREFLIGHT_ABORT
M_NOLIVE=$(mutant 's/ && \[ "\$(hc "\$PARAKEET_LIVE")" = 200 \]//')          # GATE1: drop live check
meta_detects "lost-parakeet-live detected"     "$M_NOLIVE" s_par_live 1 PREFLIGHT_ABORT
M_NOVFLOOR=$(mutant 's/\[ "\$1" -ge "\$VRAM_FLOOR" \]/true/')                # GATE2: drop below-floor check
meta_detects "lost-vram-floor detected"        "$M_NOVFLOOR" s_vram_below 1 PREFLIGHT_ABORT
M_NOHOME=$(mutant '/CANON_DIR (\$canon_abs) != pinned EXPECT_CANON_DIR/d')   # drop the home-pin assertion line
meta_detects "lost-canon-home-pin detected"    "$M_NOHOME" s_home_mismatch 1 PREFLIGHT_ABORT
M_ENVPIN=$(mutant 's#^EXPECT_CANON_DIR=/home/ericmey/voicebook-stream-deploy#EXPECT_CANON_DIR="${EXPECT_CANON_DIR:-/home/ericmey/voicebook-stream-deploy}"#')  # reintroduce env-derived pin
meta_detects "env-overridable-pin detected"    "$M_ENVPIN" s_env_pin 1 PREFLIGHT_ABORT

echo "=============================================================="
echo "PASS=$PASS FAIL=$FAIL"
[ "$FAIL" = 0 ] && echo "== OWNERSHIP_SELFTEST=PASS ==" || echo "== OWNERSHIP_SELFTEST=FAIL =="
[ "$FAIL" = 0 ]
