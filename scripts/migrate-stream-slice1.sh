#!/usr/bin/env bash
# Slice-1 migration runbook — replace the temp `voicebook-stream-qual` container
# with the managed `voicebook-stream` Compose service. Run ON mizuki in the dir
# holding docker-compose.stream.yaml + scripts/, with the guard at
# /home/ericmey/vbs-qual/watcher.sh present.
#
# The FAILURE and RECOVERY paths are as executable as the happy path:
#   * set -Eeuo pipefail + a phase-aware ERR/HUP/INT/TERM trap armed BEFORE the
#     first mutation; disabled inside rollback (no recursion).
#   * The migration guard is armed BEFORE stopping qual; its exact PID is tracked
#     and its liveness + trigger-free state asserted at every checkpoint.
#   * The old qual watcher is stopped (by exact PID) before qual is stopped, so
#     it cannot rm the rollback object.
#   * qual is STOPPED, never removed — the intact rollback.
#   * Rollback waits (bounded) for qual readiness and asserts image+health+
#     Parakeet+tts-stopped, emitting ROLLBACK_OK vs ROLLBACK_FAILED.
set -Eeuo pipefail
IMG=sha256:3b28aa8102d69b3214687a7e732dcdeca35b8a11ab0d34187e1dad3f9b4472f7
COMPOSE=docker-compose.stream.yaml
W=/home/ericmey/vbs-qual/watcher.sh
LOG=/home/ericmey/vbs-qual/migrate.log
VRAM_FLOOR=800
PARAKEET=http://127.0.0.1:9000/v1/health/ready
PHASE=preflight
MIG_PID=""
OLD_WPID=""

die() { echo "PREFLIGHT_FAIL: $*"; echo "== MIGRATE_RESULT=PREFLIGHT_ABORT =="; exit 1; }
dns_probe() { docker run --rm --network voice_default alpine sh -c \
  'wget -q -T5 -O /dev/null http://voicebook-stream:5060/healthz && echo OK || echo NO' 2>/dev/null; }
wait_health() { # $1 container  -> 0 if healthy within 150s
  local i; for i in $(seq 1 150); do
    [ "$(docker inspect -f '{{.State.Health.Status}}' "$1" 2>/dev/null)" = healthy ] && return 0
    docker ps --format '{{.Names}}' | grep -q "^$1\$" || return 1
    sleep 1
  done; return 1; }
guard_ok() { # migration guard must be alive AND never have triggered
  if [ -z "$MIG_PID" ] || ! kill -0 "$MIG_PID" 2>/dev/null; then echo "GUARD_DEAD"; return 1; fi
  if grep -q '^WATCHER_TRIGGER' "$LOG"; then echo "GUARD_TRIGGERED"; return 1; fi
  return 0; }

rollback() {
  trap - ERR HUP INT TERM        # disable trap: no recursion
  set +e
  echo "ROLLBACK: ${1:-unspecified}"
  [ -n "$MIG_PID" ] && kill "$MIG_PID" 2>/dev/null
  docker compose -f "$COMPOSE" down; dc=$?
  docker start voicebook-stream-qual; ds=$?
  # bounded wait for qual host readiness (observed ~14s startup; allow 150s)
  local ok=0 i
  for i in $(seq 1 150); do
    [ "$(curl -s -m3 -o /dev/null -w '%{http_code}' http://127.0.0.1:5060/healthz 2>/dev/null)" = 200 ] && { ok=1; break; }
    sleep 1
  done
  local qimg qrun par tts
  qimg=$(docker inspect -f '{{.Image}}' voicebook-stream-qual 2>/dev/null)
  qrun=$(docker inspect -f '{{.State.Running}}' voicebook-stream-qual 2>/dev/null)
  par=$(curl -s -m5 -o /dev/null -w '%{http_code}' "$PARAKEET")
  tts=$(docker inspect -f '{{.State.Running}}' voicebook-tts 2>/dev/null)
  echo "rollback: down_rc=$dc start_rc=$ds qual_ready=$ok qual_img=$qimg qual_running=$qrun parakeet=$par tts_running=${tts:-absent}"
  if [ "$ok" = 1 ] && [ "$qimg" = "$IMG" ] && [ "$qrun" = true ] && [ "$par" = 200 ] && [ "$tts" != true ]; then
    echo "== MIGRATE_RESULT=ROLLBACK_OK =="; exit 1
  else
    echo "== MIGRATE_RESULT=ROLLBACK_FAILED =="; exit 2
  fi
}
on_err() { [ "$PHASE" = preflight ] && { echo "aborted in preflight (no mutation)"; exit 1; }; rollback "trap: unexpected exit/signal in phase=$PHASE"; }
trap on_err ERR HUP INT TERM

echo "=== PREFLIGHT (fail-closed) ==="
docker image inspect "$IMG" >/dev/null 2>&1 || die "F1 image $IMG not present"
# rollback object must be the ACCEPTED digest, RUNNING, and healthy on 5060
[ "$(docker inspect -f '{{.Image}}' voicebook-stream-qual 2>/dev/null)" = "$IMG" ] || die "qual not on accepted digest $IMG"
[ "$(docker inspect -f '{{.State.Running}}' voicebook-stream-qual 2>/dev/null)" = true ] || die "qual not running"
[ "$(curl -s -m5 -o /dev/null -w '%{http_code}' http://127.0.0.1:5060/healthz)" = 200 ] || die "qual not healthy on 5060"
# old tts must EXIST and be STOPPED
docker inspect voicebook-tts >/dev/null 2>&1 || die "voicebook-tts container missing (rollback tier)"
[ "$(docker inspect -f '{{.State.Running}}' voicebook-tts)" = false ] || die "voicebook-tts is RUNNING (must stay stopped on 5055)"
# infra
docker network inspect voice_default >/dev/null 2>&1 || die "external network voice_default missing"
docker volume  inspect voicebook-hf-cache >/dev/null 2>&1 || die "external volume voicebook-hf-cache missing"
[ -x "$W" ] || die "guard $W not executable"
mkdir -p "$(dirname "$LOG")" && : > "$LOG" 2>/dev/null || die "log dir $(dirname "$LOG") not writable"
[ "$(curl -s -m5 -o /dev/null -w '%{http_code}' "$PARAKEET")" = 200 ] || die "Parakeet not healthy"
# reject ANY :5056 listener (loopback, wildcard, or IPv6)
if ss -ltn 2>/dev/null | awk '{print $4}' | grep -qE '(:|\.)5056$'; then die "a :5056 listener already exists"; fi
# structured compose contract (not a grep pin)
bash scripts/test-stream-compose.sh >/tmp/slice1test.out 2>&1; grep -q 'SLICE1_TEST=PASS' /tmp/slice1test.out || { cat /tmp/slice1test.out; die "structured compose test did not pass"; }
echo "OK: qual=accepted-digest+running+healthy; tts exists+stopped; net+vol present; guard+logdir ok; 5056 free; Parakeet 200; compose test PASS"

echo "=== APPLY (retire old guard, arm migration guard BEFORE stop) ==="
PHASE=apply
OLD_WPID=$(pgrep -f 'watcher.sh voicebook-stream-qual' | head -1 || true)
if [ -n "$OLD_WPID" ]; then kill "$OLD_WPID" 2>/dev/null || true; echo "stopped old qual watcher pid=$OLD_WPID (so it cannot rm the rollback)"; fi
: > "$LOG"
nohup bash "$W" voicebook-stream "$VRAM_FLOOR" 2 "$LOG" no >/dev/null 2>&1 &
MIG_PID=$!
disown "$MIG_PID" 2>/dev/null || true
sleep 3
guard_ok || rollback "migration guard failed to start"
grep -q '^SAMPLE' "$LOG" || rollback "migration guard not sampling"
echo "migration guard armed pid=$MIG_PID, sampling"
docker stop voicebook-stream-qual >/dev/null || rollback "could not stop qual"
guard_ok || rollback "guard died/tripped during stop"
docker compose -f "$COMPOSE" up -d >/dev/null || rollback "compose up failed"

echo "=== VERIFY ==="
PHASE=verify
run_img=$(docker inspect voicebook-stream --format '{{.Image}}' 2>/dev/null || true)
[ "$run_img" = "$IMG" ] || rollback "image mismatch: $run_img != $IMG"
echo "image_readback OK: $run_img"
wait_health voicebook-stream || rollback "stable did not become healthy"
guard_ok || rollback "guard died/tripped during startup"
[ "$(curl -s -m5 -o /dev/null -w '%{http_code}' http://127.0.0.1:5056/healthz)" = 200 ] || rollback "host 5056 not 200"
[ "$(dns_probe)" = OK ] || rollback "service DNS voicebook-stream:5060 unreachable from voice_default"
echo "host 5056 + service DNS OK"
code=$(curl -s -m60 -X POST http://127.0.0.1:5056/speak -H 'Content-Type: application/json' \
  -d '{"voice_id":"sumi-v1","text":"Slice one migration render check."}' -o /tmp/mig.wav -w '%{http_code}' || true)
sz=$(stat -c %s /tmp/mig.wav 2>/dev/null || echo 0); rm -f /tmp/mig.wav
{ [ "$code" = 200 ] && [ "$sz" -gt 20000 ]; } || rollback "render failed (http=$code bytes=$sz)"
guard_ok || rollback "guard died/tripped during render"
free=$(nvidia-smi --query-gpu=memory.free --format=csv,noheader,nounits | tr -d ' ')
{ [ -n "$free" ] && [ "$free" -ge "$VRAM_FLOOR" ]; } || rollback "VRAM low ($free < $VRAM_FLOOR)"
[ "$(curl -s -m5 -o /dev/null -w '%{http_code}' "$PARAKEET")" = 200 ] || rollback "Parakeet degraded during render"
echo "render OK ($sz B); VRAM free=${free}MiB; Parakeet 200"

echo "=== FORCE-RECREATE (restart-proof) ==="
PHASE=recreate
docker compose -f "$COMPOSE" up -d --force-recreate >/dev/null || rollback "force-recreate failed"
wait_health voicebook-stream || rollback "not healthy after recreate"
[ "$(docker inspect voicebook-stream --format '{{.Image}}')" = "$IMG" ] || rollback "recreate image drift"
onnet=$(docker inspect voicebook-stream --format '{{range $k,$v := .NetworkSettings.Networks}}{{$k}} {{end}}' | grep -c voice_default)
{ [ "$onnet" = 1 ] && [ "$(dns_probe)" = OK ]; } || rollback "recreate lost voice_default/DNS (onnet=$onnet)"
guard_ok || rollback "guard died/tripped during recreate"
[ "$(curl -s -m5 -o /dev/null -w '%{http_code}' "$PARAKEET")" = 200 ] || rollback "Parakeet degraded after recreate"
echo "recreate OK: immutable image, healthy, on voice_default, DNS reachable, guard clean, Parakeet 200"

echo "=== HANDOFF ==="
PHASE=done
trap - ERR HUP INT TERM
kill "$MIG_PID" 2>/dev/null || true          # migration guard done; Compose (restart+healthcheck) is the manager
echo "migration guard pid=$MIG_PID stood down"
echo "qual left STOPPED + intact as rollback: $(docker inspect -f '{{.State.Status}}' voicebook-stream-qual)"
echo "voicebook-tts remains: $(docker inspect -f '{{.State.Status}}' voicebook-tts 2>/dev/null)"
echo "== MIGRATE_RESULT=SUCCESS =="
