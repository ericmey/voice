#!/usr/bin/env bash
# Slice-1 migration runbook — replace the temporary externally-watched
# `voicebook-stream-qual` container with the managed `voicebook-stream` Compose
# service. Run ON mizuki, in the dir holding docker-compose.stream.yaml, with the
# migration guard at /home/ericmey/vbs-qual/watcher.sh present.
#
# Guarantees:
#   * FAIL-CLOSED preflight (aborts before touching anything on bad state).
#   * The guard is armed BEFORE the qual container is stopped.
#   * The qual container is STOPPED, never removed — it is the intact rollback.
#   * Auto-rollback on EVERY failure branch: remove only the new stable service,
#     `docker start` the unchanged qual container.
#   * Never runs two stream models at once (qual is stopped before stable starts).
#   * Old voicebook-tts stays stopped on 5055 throughout.
set -uo pipefail
IMG=sha256:3b28aa8102d69b3214687a7e732dcdeca35b8a11ab0d34187e1dad3f9b4472f7
COMPOSE=docker-compose.stream.yaml
W=/home/ericmey/vbs-qual/watcher.sh
LOG=/home/ericmey/vbs-qual/migrate.log
VRAM_FLOOR=800
PARAKEET=http://127.0.0.1:9000/v1/health/ready

die()  { echo "PREFLIGHT_FAIL: $*"; echo "== MIGRATE_RESULT=PREFLIGHT_ABORT =="; exit 1; }
dns_probe() { docker run --rm --network voice_default alpine sh -c \
  'wget -q -T5 -O /dev/null http://voicebook-stream:5060/healthz && echo OK || echo NO' 2>/dev/null; }
rollback() {
  echo "ROLLBACK: $*"
  docker compose -f "$COMPOSE" down >/dev/null 2>&1        # remove ONLY the new stable service
  pkill -f 'vbs-qual/watcher.sh' 2>/dev/null
  docker start voicebook-stream-qual >/dev/null 2>&1        # restart the UNCHANGED intact qual
  sleep 8
  echo "rollback_qual_health=$(curl -s -m5 -o /dev/null -w '%{http_code}' http://127.0.0.1:5060/healthz)"
  echo "voicebook-tts still: $(docker inspect -f '{{.State.Status}}' voicebook-tts 2>/dev/null)"
  echo "== MIGRATE_RESULT=ROLLED_BACK =="
  exit 1
}

echo "=== PREFLIGHT (fail-closed) ==="
docker image inspect "$IMG" >/dev/null 2>&1 || die "F1 image $IMG not present"
docker inspect voicebook-stream-qual >/dev/null 2>&1 || die "qual container missing (needed intact as rollback)"
docker network inspect voice_default >/dev/null 2>&1 || die "external network voice_default missing"
docker volume  inspect voicebook-hf-cache >/dev/null 2>&1 || die "external volume voicebook-hf-cache missing"
ss -ltn 2>/dev/null | grep -q '127.0.0.1:5056' && die "host 127.0.0.1:5056 already occupied"
[ "$(docker inspect -f '{{.State.Running}}' voicebook-tts 2>/dev/null)" = "true" ] && die "voicebook-tts is RUNNING (must stay stopped on 5055)"
[ "$(curl -s -m5 -o /dev/null -w '%{http_code}' "$PARAKEET")" = "200" ] || die "Parakeet not healthy"
docker compose -f "$COMPOSE" config >/dev/null 2>&1 || die "compose does not render"
grep -q "$IMG" "$COMPOSE" || die "compose not pinned to $IMG"
echo "OK: image present; qual intact; voice_default+volume present; 5056 free; tts stopped; Parakeet 200; compose pinned"

echo "=== APPLY (arm guard BEFORE stopping qual) ==="
: > "$LOG"
nohup bash "$W" voicebook-stream "$VRAM_FLOOR" 2 "$LOG" no >/dev/null 2>&1 & disown
sleep 2
grep -q '^SAMPLE' "$LOG" || rollback "guard did not start sampling"
docker stop voicebook-stream-qual >/dev/null 2>&1 || rollback "could not stop qual"
sleep 2
docker compose -f "$COMPOSE" up -d >/dev/null 2>&1 || rollback "compose up failed"

echo "=== VERIFY ==="
run_img=$(docker inspect voicebook-stream --format '{{.Image}}' 2>/dev/null)
[ "$run_img" = "$IMG" ] || rollback "image mismatch: $run_img != $IMG"
echo "image_readback OK: $run_img"
ready=0
for i in $(seq 1 150); do
  [ "$(docker inspect -f '{{.State.Health.Status}}' voicebook-stream 2>/dev/null)" = healthy ] && { ready=1; echo "healthy ~${i}s"; break; }
  docker ps --format '{{.Names}}' | grep -q '^voicebook-stream$' || rollback "container exited during startup"
  sleep 1
done
[ "$ready" = 1 ] || rollback "did not become healthy"
[ "$(curl -s -m5 -o /dev/null -w '%{http_code}' http://127.0.0.1:5056/healthz)" = "200" ] || rollback "host 5056 not 200"
[ "$(dns_probe)" = OK ] || rollback "service DNS voicebook-stream:5060 unreachable from voice_default"
echo "host 5056 + service DNS voicebook-stream:5060 OK"
code=$(curl -s -m60 -X POST http://127.0.0.1:5056/speak -H 'Content-Type: application/json' \
  -d '{"voice_id":"sumi-v1","text":"Slice one migration render check."}' -o /tmp/mig.wav -w '%{http_code}')
sz=$(stat -c %s /tmp/mig.wav 2>/dev/null || echo 0); rm -f /tmp/mig.wav
{ [ "$code" = 200 ] && [ "$sz" -gt 20000 ]; } || rollback "render failed (http=$code bytes=$sz)"
free=$(nvidia-smi --query-gpu=memory.free --format=csv,noheader,nounits | tr -d ' ')
{ [ -n "$free" ] && [ "$free" -ge "$VRAM_FLOOR" ]; } || rollback "VRAM low ($free < $VRAM_FLOOR)"
[ "$(curl -s -m5 -o /dev/null -w '%{http_code}' "$PARAKEET")" = "200" ] || rollback "Parakeet degraded during render"
echo "render OK ($sz B); VRAM free=${free}MiB; Parakeet 200"
docker compose -f "$COMPOSE" up -d --force-recreate >/dev/null 2>&1 || rollback "force-recreate failed"
for i in $(seq 1 150); do [ "$(docker inspect -f '{{.State.Health.Status}}' voicebook-stream 2>/dev/null)" = healthy ] && break; sleep 1; done
onnet=$(docker inspect voicebook-stream --format '{{range $k,$v := .NetworkSettings.Networks}}{{$k}} {{end}}' | grep -c voice_default)
{ [ "$onnet" = 1 ] && [ "$(dns_probe)" = OK ]; } || rollback "recreate lost voice_default/DNS (onnet=$onnet)"
echo "force-recreate OK: still on voice_default, DNS reachable (restart-proof)"

pkill -f 'vbs-qual/watcher.sh' 2>/dev/null   # migration guard stands down; Compose (restart+healthcheck) is the manager
echo "guard stood down; Compose is the service manager"
echo "qual left STOPPED + intact as rollback: $(docker inspect -f '{{.State.Status}}' voicebook-stream-qual)"
echo "voicebook-tts remains: $(docker inspect -f '{{.State.Status}}' voicebook-tts 2>/dev/null)"
echo "== MIGRATE_RESULT=SUCCESS =="
