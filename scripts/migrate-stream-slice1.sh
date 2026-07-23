#!/usr/bin/env bash
# Slice-1 migration runbook — replace `voicebook-stream-qual` with the managed
# `voicebook-stream` Compose service. Run ON mizuki in the dir holding
# docker-compose.stream.yaml + scripts/, guard at /home/ericmey/vbs-qual/watcher.sh.
#
# MODE=clean (default) runs the real migration. MODE=drill injects the
# before_render failpoint for the supervised rollback drill. MODE is validated
# in preflight BEFORE any mutation — an unknown value aborts with no mutation
# (a drill typo can NEVER fall through to the clean migration).
#
# Source-able for the no-container self-test: external commands are called bare
# so a test can shadow them; `main` runs only when executed, not sourced.
#
# Invariants held on the failure/recovery path:
#   * no-two-model: rollback proves voicebook-stream is gone BEFORE starting qual;
#   * watcher death is PROVEN (bounded TERM/kill -9/confirm) on both rollback and
#     handoff — a lingering guard still targets voicebook-stream;
#   * ROLLBACK_OK requires dc==0 AND ds==0, qual restored+guarded, tts stopped,
#     and the migration watcher dead; else ROLLBACK_FAILED.
set -Eeuo pipefail

IMG=sha256:3b28aa8102d69b3214687a7e732dcdeca35b8a11ab0d34187e1dad3f9b4472f7
COMPOSE=docker-compose.stream.yaml
PROJECT=voicebook-stream        # canonical compose project (pinned via compose `name:`); asserted on the running container
W=/home/ericmey/vbs-qual/watcher.sh
LOG=/home/ericmey/vbs-qual/migrate.log
QUAL_LOG=/home/ericmey/vbs-qual/qual.log
VRAM_FLOOR=800
PARAKEET=http://127.0.0.1:9000/v1/health/ready
DIAG_IMG="$IMG"                 # seam 4: diagnostic == the accepted immutable digest (python present), NOT a mutable tag
MODE="${MODE:-clean}"
FAILPOINTS=""                   # derived from MODE in preflight; never taken raw from env

PHASE=preflight
MIG_PID=""
OLD_WPID=""
QUAL_WPID=""

failpoint() { case " $FAILPOINTS " in *" $1 "*) echo "FAILPOINT[$1] injected"; return 1;; esac; return 0; }
die() { echo "PREFLIGHT_FAIL: $*"; echo "== MIGRATE_RESULT=PREFLIGHT_ABORT =="; exit 1; }
hc() { curl -s -m5 -o /dev/null -w '%{http_code}' "$1" 2>/dev/null; }
# seam 4: run the probe inside the accepted immutable image by digest, python stdlib (no wget dependency, no mutable tag)
dns_probe() { docker run --rm --pull=never --network voice_default --entrypoint python "$DIAG_IMG" -c \
  'import urllib.request; urllib.request.urlopen("http://voicebook-stream:5060/healthz",timeout=5); print("OK")' 2>/dev/null || echo NO; }
arm_watcher() { : > "$2"; nohup bash "$W" "$1" "$VRAM_FLOOR" 2 "$2" no >/dev/null 2>&1 & local p=$!; disown "$p" 2>/dev/null || true; echo "$p"; }
# seam 2: PROVE the watcher is dead — bounded TERM, then kill -9, then confirm
stop_watcher() {
  local p="$1" i
  [ -z "$p" ] && return 0
  kill "$p" 2>/dev/null || true
  for i in 1 2 3 4 5; do kill -0 "$p" 2>/dev/null || return 0; sleep 1; done
  kill -9 "$p" 2>/dev/null || true
  for i in 1 2 3; do kill -0 "$p" 2>/dev/null || return 0; sleep 1; done
  return 1
}
guard_ok() { # $1 pid  $2 log
  if [ -z "$1" ] || ! kill -0 "$1" 2>/dev/null; then echo "GUARD_DEAD(pid=$1)"; return 1; fi
  if grep -q '^WATCHER_TRIGGER' "$2" 2>/dev/null; then echo "GUARD_TRIGGERED"; return 1; fi
  return 0
}
wait_health() { # $1 container — guard-checked each iteration
  local i
  for i in $(seq 1 150); do
    [ "$(docker inspect -f '{{.State.Health.Status}}' "$1" 2>/dev/null)" = healthy ] && return 0
    docker ps --format '{{.Names}}' | grep -q "^$1\$" || { echo "container $1 exited during startup"; return 1; }
    guard_ok "$MIG_PID" "$LOG" || { echo "migration guard failed during wait_health"; return 2; }
    sleep 1
  done
  return 1
}

rollback() {
  trap - ERR HUP INT TERM
  set +e
  echo "ROLLBACK: ${1:-unspecified}"
  # seam 2: stop the migration guard and PROVE it dead before touching state
  stop_watcher "$MIG_PID"; local mwdead=$?
  [ "$mwdead" = 0 ] && echo "migration watcher $MIG_PID confirmed dead" || echo "WARN: migration watcher $MIG_PID NOT confirmed dead"
  docker compose -f "$COMPOSE" down >/dev/null 2>&1; local dc=$?
  # seam 1: no-two-model, TRI-STATE FAIL-CLOSED — and NEVER infer absence from a
  # FAILED command. A `docker inspect` that errors (daemon/transient/permission)
  # is not proof the container is gone. Use a readback whose SUCCESSFUL execution
  # distinguishes absence (zero exact rows) from state; a nonzero rc is UNKNOWN.
  local rows rrc stable_gone
  rows=$(docker ps -a --filter 'name=^voicebook-stream$' --format '{{.State}}' 2>/dev/null); rrc=$?
  if [ "$rrc" != 0 ]; then stable_gone=0                 # readback FAILED -> UNKNOWN -> unsafe (never infer absence)
  elif [ -z "$rows" ]; then stable_gone=1               # rc0 + zero exact rows -> definitively absent -> safe
  elif [ "$rows" = exited ]; then stable_gone=1         # rc0 + single exited row -> definitively non-running -> safe
  else stable_gone=0; fi                                # running/restarting/created/removing/dead/paused/ambiguous/multi -> unsafe
  if [ "$stable_gone" != 1 ]; then
    echo "ROLLBACK_ABORT_NO_START: voicebook-stream not PROVEN gone (readback_rc=$rrc rows='${rows:-<none>}') — refusing to start qual (no-two-model, fail-closed)"
    echo "== MIGRATE_RESULT=ROLLBACK_FAILED =="; exit 2
  fi
  docker start voicebook-stream-qual >/dev/null 2>&1; local ds=$?
  local ok=0 i
  for i in $(seq 1 150); do [ "$(hc http://127.0.0.1:5060/healthz)" = 200 ] && { ok=1; break; }; sleep 1; done
  # seam 3: re-arm the qual watcher and PROVE it (restore the accepted pre-state)
  QUAL_WPID=$(arm_watcher voicebook-stream-qual "$QUAL_LOG"); sleep 3
  local gstat=BAD
  if guard_ok "$QUAL_WPID" "$QUAL_LOG" >/dev/null 2>&1 && grep -q '^SAMPLE' "$QUAL_LOG"; then gstat=OK; fi
  local qimg qrun par ttsexist ttsr
  qimg=$(docker inspect -f '{{.Image}}' voicebook-stream-qual 2>/dev/null)
  qrun=$(docker inspect -f '{{.State.Running}}' voicebook-stream-qual 2>/dev/null)
  par=$(hc "$PARAKEET")
  docker inspect voicebook-tts >/dev/null 2>&1 && ttsexist=1 || ttsexist=0
  ttsr=$(docker inspect -f '{{.State.Running}}' voicebook-tts 2>/dev/null)
  echo "rollback: mwdead=$mwdead down_rc=$dc start_rc=$ds qual_ready=$ok qual_img=$qimg qual_running=$qrun parakeet=$par tts_exist=$ttsexist tts_running=${ttsr:-NA} qual_watcher=$gstat(pid=$QUAL_WPID)"
  if [ "$mwdead" = 0 ] && [ "$dc" = 0 ] && [ "$ds" = 0 ] && [ "$ok" = 1 ] && [ "$qimg" = "$IMG" ] && [ "$qrun" = true ] \
     && [ "$par" = 200 ] && [ "$ttsexist" = 1 ] && [ "$ttsr" = false ] && [ "$gstat" = OK ]; then
    echo "== MIGRATE_RESULT=ROLLBACK_OK =="; exit 1
  fi
  echo "== MIGRATE_RESULT=ROLLBACK_FAILED =="; exit 2
}
on_err() { if [ "$PHASE" = preflight ]; then echo "aborted in preflight (no mutation)"; exit 1; fi; rollback "trap: unexpected exit/signal in phase=$PHASE"; }

main() {
  echo "=== PREFLIGHT (fail-closed) ==="
  # validate MODE before ANY mutation — a drill typo cannot run the clean migration
  case "$MODE" in
    clean) FAILPOINTS="";;
    drill) FAILPOINTS="before_render";;
    *) die "MODE must be 'clean' or 'drill' (got '$MODE')";;
  esac
  echo "MODE=$MODE (failpoints='$FAILPOINTS')"
  docker image inspect "$IMG" >/dev/null 2>&1 || die "F1/diagnostic image $IMG not present"   # seam 4: covers DIAG_IMG (== IMG)
  [ "$(docker inspect -f '{{.Image}}' voicebook-stream-qual 2>/dev/null)" = "$IMG" ] || die "qual not on accepted digest"
  [ "$(docker inspect -f '{{.State.Running}}' voicebook-stream-qual 2>/dev/null)" = true ] || die "qual not running"
  [ "$(hc http://127.0.0.1:5060/healthz)" = 200 ] || die "qual not healthy on 5060"
  docker inspect voicebook-tts >/dev/null 2>&1 || die "voicebook-tts container missing (rollback tier)"
  [ "$(docker inspect -f '{{.State.Running}}' voicebook-tts)" = false ] || die "voicebook-tts is RUNNING (must stay stopped)"
  docker network inspect voice_default >/dev/null 2>&1 || die "external network voice_default missing"
  docker volume  inspect voicebook-hf-cache >/dev/null 2>&1 || die "external volume voicebook-hf-cache missing"
  [ -x "$W" ] || die "guard $W not executable"
  mkdir -p "$(dirname "$LOG")" && : > "$LOG" 2>/dev/null || die "log dir not writable"
  [ "$(hc "$PARAKEET")" = 200 ] || die "Parakeet not healthy"
  if ss -ltn 2>/dev/null | awk '{print $4}' | grep -qE '(:|\.)5056$'; then die "a :5056 listener already exists"; fi
  local n; n=$(pgrep -fc 'watcher.sh voicebook-stream-qual' 2>/dev/null || true)
  [ "$n" = 1 ] || die "expected exactly 1 qual watcher, found ${n:-0}"
  OLD_WPID=$(pgrep -f 'watcher.sh voicebook-stream-qual' | head -1)
  # structured compose contract — require BOTH exit 0 AND the PASS sentinel
  local tc; if bash scripts/test-stream-compose.sh >/tmp/slice1test.out 2>&1; then tc=0; else tc=$?; fi
  if [ "$tc" != 0 ] || ! grep -q 'SLICE1_TEST=PASS' /tmp/slice1test.out; then cat /tmp/slice1test.out; die "structured compose test failed (rc=$tc)"; fi
  echo "PREFLIGHT OK (qual=accepted+running+healthy, tts exists+stopped, net/vol/guard/log ok, 5056 free, 1 qual watcher pid=$OLD_WPID, compose test rc=0+PASS)"

  echo "=== APPLY (arm trap, retire old guard by exact PID, arm migration guard BEFORE stop) ==="
  PHASE=apply
  trap on_err ERR HUP INT TERM
  kill "$OLD_WPID" 2>/dev/null || true
  local k; for k in 1 2 3 4 5; do kill -0 "$OLD_WPID" 2>/dev/null || break; sleep 1; done
  kill -0 "$OLD_WPID" 2>/dev/null && rollback "old qual watcher $OLD_WPID would not terminate"
  echo "old qual watcher $OLD_WPID terminated + confirmed dead"
  MIG_PID=$(arm_watcher voicebook-stream "$LOG"); sleep 3
  guard_ok "$MIG_PID" "$LOG" || rollback "migration guard failed to start"
  grep -q '^SAMPLE' "$LOG" || rollback "migration guard not sampling"
  echo "migration guard pid=$MIG_PID armed + sampling"
  failpoint after_arm || rollback "failpoint after_arm"
  docker stop voicebook-stream-qual >/dev/null || rollback "could not stop qual"
  guard_ok "$MIG_PID" "$LOG" || rollback "guard died/tripped during stop"
  failpoint after_stop || rollback "failpoint after_stop"
  docker compose -f "$COMPOSE" up -d >/dev/null || rollback "compose up failed"

  echo "=== VERIFY ==="
  PHASE=verify
  local run_img; run_img=$(docker inspect voicebook-stream --format '{{.Image}}' 2>/dev/null || true)
  [ "$run_img" = "$IMG" ] || rollback "image mismatch: $run_img != $IMG"
  echo "image_readback OK: $run_img"
  wait_health voicebook-stream || rollback "stable did not become healthy"
  guard_ok "$MIG_PID" "$LOG" || rollback "guard died/tripped after startup"
  # lifecycle authority: the running container MUST carry the canonical project label
  [ "$(docker inspect -f '{{ index .Config.Labels "com.docker.compose.project" }}' voicebook-stream 2>/dev/null)" = "$PROJECT" ] || rollback "compose project label != $PROJECT (lifecycle owner not canonical)"
  echo "project label OK: $PROJECT"
  [ "$(hc http://127.0.0.1:5056/healthz)" = 200 ] || rollback "host 5056 not 200"
  [ "$(dns_probe)" = OK ] || rollback "service DNS voicebook-stream:5060 unreachable"
  echo "host 5056 + service DNS OK"
  failpoint before_render || rollback "failpoint before_render (drill point)"
  local code sz
  code=$(curl -s -m60 -X POST http://127.0.0.1:5056/speak -H 'Content-Type: application/json' \
    -d '{"voice_id":"sumi-v1","text":"Slice one migration render check."}' -o /tmp/mig.wav -w '%{http_code}' || true)
  sz=$(stat -c %s /tmp/mig.wav 2>/dev/null || echo 0); rm -f /tmp/mig.wav
  { [ "$code" = 200 ] && [ "$sz" -gt 20000 ]; } || rollback "render failed (http=$code bytes=$sz)"
  guard_ok "$MIG_PID" "$LOG" || rollback "guard died/tripped during render"
  local free; free=$(nvidia-smi --query-gpu=memory.free --format=csv,noheader,nounits | tr -d ' ')
  { [ -n "$free" ] && [ "$free" -ge "$VRAM_FLOOR" ]; } || rollback "VRAM low ($free < $VRAM_FLOOR)"
  [ "$(hc "$PARAKEET")" = 200 ] || rollback "Parakeet degraded during render"
  echo "render OK ($sz B); VRAM free=${free}MiB; Parakeet 200"

  echo "=== FORCE-RECREATE (restart-proof) ==="
  PHASE=recreate
  docker compose -f "$COMPOSE" up -d --force-recreate >/dev/null || rollback "force-recreate failed"
  wait_health voicebook-stream || rollback "not healthy after recreate"
  [ "$(docker inspect voicebook-stream --format '{{.Image}}')" = "$IMG" ] || rollback "recreate image drift"
  local onnet; onnet=$(docker inspect voicebook-stream --format '{{range $k,$v := .NetworkSettings.Networks}}{{$k}} {{end}}' | grep -c voice_default)
  { [ "$onnet" = 1 ] && [ "$(dns_probe)" = OK ]; } || rollback "recreate lost voice_default/DNS (onnet=$onnet)"
  [ "$(docker inspect -f '{{ index .Config.Labels "com.docker.compose.project" }}' voicebook-stream 2>/dev/null)" = "$PROJECT" ] || rollback "recreate project label != $PROJECT"
  guard_ok "$MIG_PID" "$LOG" || rollback "guard died/tripped after recreate"
  [ "$(hc "$PARAKEET")" = 200 ] || rollback "Parakeet degraded after recreate"
  echo "recreate OK: immutable image, healthy, voice_default, DNS, guard clean, Parakeet 200"

  echo "=== HANDOFF ==="
  PHASE=done
  trap - ERR HUP INT TERM
  # seam 2 (Bar B #3): the migration watcher MUST die. If it will not, we must NOT
  # leave the managed service running under an orphan watcher that can later delete
  # it — RECOVER to qual via verified rollback (which reports ROLLBACK_FAILED
  # because the watcher remains unkillable), never abandon a half-owned state.
  if ! stop_watcher "$MIG_PID"; then
    echo "HANDOFF ANOMALY: migration watcher $MIG_PID would not die; recovering to qual (never leave stable under an orphan watcher)"
    rollback "handoff: migration watcher $MIG_PID unkillable"
  fi
  echo "migration watcher $MIG_PID confirmed dead"
  # final handoff assertion set — prove the exact end-state before declaring SUCCESS
  [ "$(docker inspect voicebook-stream --format '{{.Image}}' 2>/dev/null)" = "$IMG" ] || rollback "handoff assert: stable image != accepted digest"
  [ "$(docker inspect -f '{{.State.Health.Status}}' voicebook-stream 2>/dev/null)" = healthy ] || rollback "handoff assert: stable not healthy"
  [ "$(docker inspect -f '{{ index .Config.Labels "com.docker.compose.project" }}' voicebook-stream 2>/dev/null)" = "$PROJECT" ] || rollback "handoff assert: project label != $PROJECT"
  [ "$(docker inspect -f '{{.State.Status}}' voicebook-stream-qual 2>/dev/null)" = exited ] || rollback "handoff assert: qual not exactly stopped"
  [ "$(docker inspect -f '{{.State.Running}}' voicebook-tts 2>/dev/null)" = false ] || rollback "handoff assert: tts not stopped"
  [ "$(hc "$PARAKEET")" = 200 ] || rollback "handoff assert: Parakeet not 200"
  echo "handoff asserts OK: stable digest+healthy, qual exactly stopped, tts stopped, Parakeet 200, migration watcher dead; Compose is the service manager"
  echo "== MIGRATE_RESULT=SUCCESS =="
}

if [ "${BASH_SOURCE[0]}" = "${0}" ]; then main "$@"; fi
