#!/usr/bin/env bash
# IN-PLACE COMPOSE PROJECT-OWNERSHIP MIGRATION  (ARTIFACT / PLAN — review before run)
#
# The clean migration left voicebook-stream RUNNING but labelled with a project
# derived from the staging dir (com.docker.compose.project=vbs-drill-a6a9c4e). A
# staging dir must not be the durable lifecycle authority. This runbook moves the
# running container to the canonical pinned project `voicebook-stream`, operated
# from a DURABLE canonical home, with no artifact edits.
#
# It is a brief-blip operation: container_name is fixed (`voicebook-stream`), so
# the old-project container is removed before the canonical-project one is created
# (Compose cannot own the same container_name under two projects). The service is
# NOT live yet, so a few seconds of gap is acceptable; still, it is fail-closed and
# rolls back to a healthy serving container.
#
# Usage:  migrate-project-ownership.sh <CANON_DIR>
#   CANON_DIR must hold the hash-identical a6a9c4e docker-compose.stream.yaml WITH
#   the `name: voicebook-stream` pin. Establish it first (durable home, e.g.
#   ~/voice or a repo checkout), mirror the artifact, verify the hash — do NOT
#   point this at the vbs-drill staging dir.
#
# Source-able for a no-container self-test (BASH_SOURCE guard); `main` runs only
# when executed.
set -Eeuo pipefail

IMG=sha256:3b28aa8102d69b3214687a7e732dcdeca35b8a11ab0d34187e1dad3f9b4472f7
PROJECT=voicebook-stream
EXPECT_COMPOSE_SHA=c8789f0d3462a0901eedf200438208884055457498de218863777a1a1ceea042
PARAKEET=http://127.0.0.1:9000/v1/health/ready
CANON_DIR="${1:-}"
COMPOSE=""
OLD_PROJECT=""
PHASE=preflight

die() { echo "PREFLIGHT_FAIL: $*"; echo "== OWNERSHIP_RESULT=PREFLIGHT_ABORT =="; exit 1; }
hc() { curl -s -m5 -o /dev/null -w '%{http_code}' "$1" 2>/dev/null; }
proj_label() { docker inspect -f '{{ index .Config.Labels "com.docker.compose.project" }}' "$1" 2>/dev/null; }
health() { docker inspect -f '{{.State.Health.Status}}' "$1" 2>/dev/null; }
dns_probe() { docker run --rm --pull=never --network voice_default --entrypoint python "$IMG" -c \
  'import urllib.request; urllib.request.urlopen("http://voicebook-stream:5060/healthz",timeout=5); print("OK")' 2>/dev/null || echo NO; }
# TRI-STATE fail-closed absence check (never infer absence from a failed command)
stable_gone() {
  local rows rrc
  rows=$(docker ps -a --filter 'name=^voicebook-stream$' --format '{{.State}}' 2>/dev/null); rrc=$?
  [ "$rrc" != 0 ] && { echo unknown; return; }
  [ -z "$rows" ] && { echo yes; return; }
  echo no
}
wait_health() { local i; for i in $(seq 1 150); do [ "$(health "$1")" = healthy ] && return 0; docker ps --format '{{.Names}}' | grep -q "^$1\$" || return 1; sleep 1; done; return 1; }

rollback() {
  trap - ERR HUP INT TERM; set +e
  echo "OWNERSHIP_ROLLBACK: ${1:-unspecified}"
  # goal: a healthy SERVING voicebook-stream. Re-create under canonical (idempotent, same a6a9c4e image/config).
  docker compose -f "$COMPOSE" up -d >/dev/null 2>&1
  local ok=0 i
  for i in $(seq 1 150); do [ "$(health voicebook-stream)" = healthy ] && { ok=1; break; }; sleep 1; done
  local lbl; lbl=$(proj_label voicebook-stream)
  if [ "$ok" = 1 ]; then
    echo "rollback: voicebook-stream healthy, project=$lbl (service restored)"
    echo "== OWNERSHIP_RESULT=ROLLBACK_OK =="; exit 1
  fi
  # last-resort tier: bring the dormant qual back so 5060 serves; flag for a human
  docker start voicebook-stream-qual >/dev/null 2>&1
  echo "rollback: canonical up FAILED; started dormant qual as interim (5060). MANUAL intervention required."
  echo "== OWNERSHIP_RESULT=ROLLBACK_FAILED =="; exit 2
}
on_err() { [ "$PHASE" = preflight ] && { echo "aborted in preflight (no mutation)"; exit 1; }; rollback "trap: unexpected exit/signal in phase=$PHASE"; }

main() {
  echo "=== PREFLIGHT (fail-closed) ==="
  [ -n "$CANON_DIR" ] || die "usage: migrate-project-ownership.sh <CANON_DIR with pinned a6a9c4e compose>"
  COMPOSE="$CANON_DIR/docker-compose.stream.yaml"
  [ -f "$COMPOSE" ] || die "no compose at $COMPOSE"
  local sha; sha=$(shasum -a 256 "$COMPOSE" | awk '{print $1}')
  [ "$sha" = "$EXPECT_COMPOSE_SHA" ] || die "compose hash $sha != a6a9c4e $EXPECT_COMPOSE_SHA"
  local rendered; rendered=$(docker compose -f "$COMPOSE" config --format json 2>/dev/null | python3 -c 'import json,sys;print(json.load(sys.stdin).get("name",""))')
  [ "$rendered" = "$PROJECT" ] || die "canonical compose renders project '$rendered' != $PROJECT (pin missing)"
  # the canonical dir must NOT be the staging dir
  case "$CANON_DIR" in *vbs-drill*) die "CANON_DIR must be a durable home, not the vbs-drill staging dir";; esac
  # running service must exist, be the accepted digest, healthy, serving
  docker inspect voicebook-stream >/dev/null 2>&1 || die "voicebook-stream not running (nothing to re-own)"
  [ "$(docker inspect -f '{{.Image}}' voicebook-stream)" = "$IMG" ] || die "running stable not accepted digest"
  [ "$(health voicebook-stream)" = healthy ] || die "running stable not healthy"
  [ "$(hc http://127.0.0.1:5056/healthz)" = 200 ] || die "host 5056 not 200"
  [ "$(dns_probe)" = OK ] || die "service DNS not reachable"
  OLD_PROJECT=$(proj_label voicebook-stream)
  [ -n "$OLD_PROJECT" ] || die "could not read current project label"
  [ "$OLD_PROJECT" != "$PROJECT" ] || { echo "already canonical (project=$PROJECT); nothing to do"; echo "== OWNERSHIP_RESULT=ALREADY_CANONICAL =="; exit 0; }
  # rollback tier + peers intact
  [ "$(docker inspect -f '{{.State.Status}}' voicebook-stream-qual 2>/dev/null)" = exited ] || die "qual not stopped-intact (rollback tier)"
  [ "$(docker inspect -f '{{.Image}}' voicebook-stream-qual 2>/dev/null)" = "$IMG" ] || die "qual not accepted digest"
  [ "$(docker inspect -f '{{.State.Running}}' voicebook-tts 2>/dev/null)" = false ] || die "voicebook-tts not stopped"
  [ "$(hc "$PARAKEET")" = 200 ] || die "Parakeet not ready"
  local vram; vram=$(nvidia-smi --query-gpu=memory.free --format=csv,noheader,nounits | tr -d ' ')
  echo "PREFLIGHT OK (running stable healthy+accepted, OLD project=$OLD_PROJECT -> canonical $PROJECT, qual stopped-intact, tts stopped, Parakeet 200, VRAM ${vram}MiB)"

  echo "=== MIGRATE (remove old-project container, recreate under canonical) ==="
  PHASE=migrate
  trap on_err ERR HUP INT TERM
  # remove the mislabelled container via ITS project (‑p overrides the file name:)
  docker compose -p "$OLD_PROJECT" -f "$COMPOSE" down >/dev/null 2>&1 || rollback "down of old project $OLD_PROJECT failed"
  # prove it is definitively gone before recreating (tri-state fail-closed)
  local g; g=$(stable_gone)
  [ "$g" = yes ] || rollback "voicebook-stream not PROVEN gone after down (state=$g)"
  # create under the canonical project (compose name: -> project voicebook-stream)
  docker compose -f "$COMPOSE" up -d >/dev/null 2>&1 || rollback "canonical up failed"

  echo "=== VERIFY ==="
  PHASE=verify
  wait_health voicebook-stream || rollback "canonical stable did not become healthy"
  [ "$(proj_label voicebook-stream)" = "$PROJECT" ] || rollback "project label != $PROJECT after re-own"
  [ "$(docker inspect -f '{{.Image}}' voicebook-stream)" = "$IMG" ] || rollback "image drift after re-own"
  [ "$(hc http://127.0.0.1:5056/healthz)" = 200 ] || rollback "host 5056 not 200 after re-own"
  [ "$(dns_probe)" = OK ] || rollback "service DNS lost after re-own"
  local code sz
  code=$(curl -s -m60 -X POST http://127.0.0.1:5056/speak -H 'Content-Type: application/json' \
    -d '{"voice_id":"sumi-v1","text":"Ownership migration render check."}' -o /tmp/own.wav -w '%{http_code}' || true)
  sz=$(stat -c %s /tmp/own.wav 2>/dev/null || echo 0); rm -f /tmp/own.wav
  { [ "$code" = 200 ] && [ "$sz" -gt 20000 ]; } || rollback "render failed after re-own (http=$code bytes=$sz)"
  [ "$(hc "$PARAKEET")" = 200 ] || rollback "Parakeet degraded after re-own"
  [ "$(docker inspect -f '{{.State.Status}}' voicebook-stream-qual 2>/dev/null)" = exited ] || rollback "qual disturbed"
  [ "$(docker inspect -f '{{.State.Running}}' voicebook-tts 2>/dev/null)" = false ] || rollback "tts disturbed"

  echo "=== DONE ==="
  PHASE=done
  trap - ERR HUP INT TERM
  echo "re-owned: voicebook-stream project=$PROJECT (was $OLD_PROJECT), healthy, accepted digest, render OK, DNS OK, qual+tts undisturbed"
  echo "canonical lifecycle home: $CANON_DIR (durable). Old staging dir may now be retired."
  echo "== OWNERSHIP_RESULT=SUCCESS =="
}

if [ "${BASH_SOURCE[0]}" = "${0}" ]; then main "$@"; fi
