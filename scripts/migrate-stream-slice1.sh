#!/usr/bin/env bash
# Slice-1 migration runbook — replace `voicebook-stream-qual` with the managed
# `voicebook-stream` Compose service. Run ON mizuki in the dir holding
# docker-compose.stream.yaml + scripts/, with the guard at
# /home/ericmey/vbs-qual/watcher.sh present.
#
# Source-able for the no-container fault-injection self-test:
#   * external commands (docker/curl/ss/nvidia-smi/pgrep/kill/sleep/stat) are
#     called bare so a test can shadow them with mock functions;
#   * `main` runs only when EXECUTED, not when sourced;
#   * FAILPOINTS (space-separated allowlist, default empty) injects deterministic
#     failures at named points for the supervised rollback drill.
#
# Failure/recovery is as executable as the happy path: set -Eeuo pipefail + a
# phase-aware trap armed after preflight; guard liveness checked every loop; a
# VERIFIED rollback that re-arms + proves the qual watcher and distinguishes
# ROLLBACK_OK from ROLLBACK_FAILED.
set -Eeuo pipefail

IMG=sha256:3b28aa8102d69b3214687a7e732dcdeca35b8a11ab0d34187e1dad3f9b4472f7
COMPOSE=docker-compose.stream.yaml
W=/home/ericmey/vbs-qual/watcher.sh
LOG=/home/ericmey/vbs-qual/migrate.log
QUAL_LOG=/home/ericmey/vbs-qual/qual.log
VRAM_FLOOR=800
PARAKEET=http://127.0.0.1:9000/v1/health/ready
DIAG_IMG=alpine
FAILPOINTS="${FAILPOINTS:-}"

PHASE=preflight
MIG_PID=""
OLD_WPID=""
QUAL_WPID=""

failpoint() { case " $FAILPOINTS " in *" $1 "*) echo "FAILPOINT[$1] injected"; return 1;; esac; return 0; }
die() { echo "PREFLIGHT_FAIL: $*"; echo "== MIGRATE_RESULT=PREFLIGHT_ABORT =="; exit 1; }
hc() { curl -s -m5 -o /dev/null -w '%{http_code}' "$1" 2>/dev/null; }
dns_probe() { docker run --rm --pull=never --network voice_default "$DIAG_IMG" sh -c \
  'wget -q -T5 -O /dev/null http://voicebook-stream:5060/healthz && echo OK || echo NO' 2>/dev/null; }
arm_watcher() { : > "$2"; nohup bash "$W" "$1" "$VRAM_FLOOR" 2 "$2" no >/dev/null 2>&1 & local p=$!; disown "$p" 2>/dev/null || true; echo "$p"; }
guard_ok() { # $1 pid  $2 log
  if [ -z "$1" ] || ! kill -0 "$1" 2>/dev/null; then echo "GUARD_DEAD(pid=$1)"; return 1; fi
  if grep -q '^WATCHER_TRIGGER' "$2" 2>/dev/null; then echo "GUARD_TRIGGERED"; return 1; fi
  return 0
}
wait_health() { # $1 container — guard-checked each iteration (seam 4)
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
  [ -n "$MIG_PID" ] && kill "$MIG_PID" 2>/dev/null
  docker compose -f "$COMPOSE" down >/dev/null 2>&1; local dc=$?
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
  docker inspect voicebook-tts >/dev/null 2>&1 && ttsexist=1 || ttsexist=0   # seam 2
  ttsr=$(docker inspect -f '{{.State.Running}}' voicebook-tts 2>/dev/null)
  echo "rollback: down_rc=$dc start_rc=$ds qual_ready=$ok qual_img=$qimg qual_running=$qrun parakeet=$par tts_exist=$ttsexist tts_running=${ttsr:-NA} qual_watcher=$gstat(pid=$QUAL_WPID)"
  # seam 1+2+3: success requires down AND start succeeded, qual restored+guarded, tts exists+stopped
  if [ "$dc" = 0 ] && [ "$ds" = 0 ] && [ "$ok" = 1 ] && [ "$qimg" = "$IMG" ] && [ "$qrun" = true ] \
     && [ "$par" = 200 ] && [ "$ttsexist" = 1 ] && [ "$ttsr" = false ] && [ "$gstat" = OK ]; then
    echo "== MIGRATE_RESULT=ROLLBACK_OK =="; exit 1
  fi
  echo "== MIGRATE_RESULT=ROLLBACK_FAILED =="; exit 2
}
on_err() { if [ "$PHASE" = preflight ]; then echo "aborted in preflight (no mutation)"; exit 1; fi; rollback "trap: unexpected exit/signal in phase=$PHASE"; }

main() {
  echo "=== PREFLIGHT (fail-closed) ==="
  docker image inspect "$IMG" >/dev/null 2>&1 || die "F1 image $IMG not present"
  docker image inspect "$DIAG_IMG" >/dev/null 2>&1 || die "diagnostic image $DIAG_IMG not present (dns_probe uses --pull=never)"   # seam 8
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
  local n; n=$(pgrep -fc 'watcher.sh voicebook-stream-qual' 2>/dev/null || true)   # seam 5
  [ "$n" = 1 ] || die "expected exactly 1 qual watcher, found ${n:-0}"
  OLD_WPID=$(pgrep -f 'watcher.sh voicebook-stream-qual' | head -1)
  bash scripts/test-stream-compose.sh >/tmp/slice1test.out 2>&1 || true   # seam 9: wrapped, diagnostic reachable
  if ! grep -q 'SLICE1_TEST=PASS' /tmp/slice1test.out; then cat /tmp/slice1test.out; die "structured compose test did not pass"; fi
  echo "PREFLIGHT OK (qual=accepted+running+healthy, tts exists+stopped, net/vol/guard/log ok, 5056 free, Parakeet 200, 1 qual watcher pid=$OLD_WPID, compose test PASS)"

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
  [ "$(hc http://127.0.0.1:5056/healthz)" = 200 ] || rollback "host 5056 not 200"
  [ "$(dns_probe)" = OK ] || rollback "service DNS voicebook-stream:5060 unreachable"
  echo "host 5056 + service DNS OK"
  failpoint before_render || rollback "failpoint before_render (drill point)"   # seam 6
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
  guard_ok "$MIG_PID" "$LOG" || rollback "guard died/tripped after recreate"
  [ "$(hc "$PARAKEET")" = 200 ] || rollback "Parakeet degraded after recreate"
  echo "recreate OK: immutable image, healthy, voice_default, DNS, guard clean, Parakeet 200"

  echo "=== HANDOFF ==="
  PHASE=done
  trap - ERR HUP INT TERM
  kill "$MIG_PID" 2>/dev/null || true
  echo "migration guard pid=$MIG_PID stood down; Compose is the service manager"
  echo "qual STOPPED + intact: $(docker inspect -f '{{.State.Status}}' voicebook-stream-qual)"
  echo "voicebook-tts: $(docker inspect -f '{{.State.Status}}' voicebook-tts 2>/dev/null)"
  echo "== MIGRATE_RESULT=SUCCESS =="
}

if [ "${BASH_SOURCE[0]}" = "${0}" ]; then main "$@"; fi
