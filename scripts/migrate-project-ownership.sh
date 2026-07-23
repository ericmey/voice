#!/usr/bin/env bash
# IN-PLACE COMPOSE PROJECT-OWNERSHIP MIGRATION  (ARTIFACT / PLAN — review before run)
#
# The clean migration left voicebook-stream RUNNING but labelled with a project
# derived from the staging dir (com.docker.compose.project=vbs-drill-a6a9c4e). A
# staging dir must not be the durable lifecycle authority. This runbook moves the
# running container to the canonical pinned project `voicebook-stream`, operated
# from a DURABLE canonical home, with no artifact edits.
#
# Brief-blip op: container_name is fixed, so the old-project container is removed
# before the canonical one is created. Fail-closed; rollback proves ONE complete
# restored tier (A canonical / B old-project / C qual) or reports ROLLBACK_FAILED,
# and NEVER starts qual beside a running/unknown stable (no-two-model).
#
# Usage:  migrate-project-ownership.sh <CANON_DIR>
#   CANON_DIR: the DURABLE home holding the pinned (81dc60e) docker-compose.stream.yaml.
#   It is physically resolved (realpath) and must NOT be the drill staging dir.
#
# Source-able for a no-container self-test (BASH_SOURCE guard); `main` runs only
# when executed.
set -Eeuo pipefail

IMG=sha256:3b28aa8102d69b3214687a7e732dcdeca35b8a11ab0d34187e1dad3f9b4472f7
PROJECT=voicebook-stream
EXPECT_COMPOSE_SHA=cb3dc23449aafebbc5e4c4d2d3c16c1adc8d2cba2bec4e781b2c0f1fc12f3899   # the PINNED (81dc60e) compose
STAGING_DIR="${STAGING_DIR:-$HOME/vbs-drill-a6a9c4e}"
PARAKEET=http://127.0.0.1:9000/v1/health/ready
CANON_DIR="${1:-}"
COMPOSE=""
OLD_PROJECT=""
PHASE=preflight

die() { echo "PREFLIGHT_FAIL: $*"; echo "== OWNERSHIP_RESULT=PREFLIGHT_ABORT =="; exit 1; }
hc() { curl -s -m5 -o /dev/null -w '%{http_code}' "$1" 2>/dev/null; }
proj_label() { docker inspect -f '{{ index .Config.Labels "com.docker.compose.project" }}' "$1" 2>/dev/null; }
health() { docker inspect -f '{{.State.Health.Status}}' "$1" 2>/dev/null; }
running() { docker inspect -f '{{.State.Running}}' "$1" 2>/dev/null; }
dns_probe() { docker run --rm --pull=never --network voice_default --entrypoint python "$IMG" -c \
  'import urllib.request; urllib.request.urlopen("http://voicebook-stream:5060/healthz",timeout=5); print("OK")' 2>/dev/null || echo NO; }
# TRI-STATE fail-closed: yes=definitively absent, no=present, unknown=readback errored (never infer absence)
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
  # try to restore a healthy SERVING voicebook-stream under canonical (idempotent, same image/config)
  docker compose -f "$COMPOSE" up -d >/dev/null 2>&1; local up_rc=$?
  local ok=0 i; for i in $(seq 1 150); do [ "$(health voicebook-stream)" = healthy ] && { ok=1; break; }; sleep 1; done
  local lbl img h5056 dns par ttsr qstat
  lbl=$(proj_label voicebook-stream); img=$(docker inspect -f '{{.Image}}' voicebook-stream 2>/dev/null)
  h5056=$(hc http://127.0.0.1:5056/healthz); dns=$(dns_probe); par=$(hc "$PARAKEET")
  ttsr=$(running voicebook-tts); qstat=$(docker inspect -f '{{.State.Status}}' voicebook-stream-qual 2>/dev/null)
  # STATE A — canonical stable fully restored
  if [ "$up_rc" = 0 ] && [ "$ok" = 1 ] && [ "$img" = "$IMG" ] && [ "$lbl" = "$PROJECT" ] \
     && [ "$h5056" = 200 ] && [ "$dns" = OK ] && [ "$par" = 200 ] && [ "$ttsr" = false ] && [ "$qstat" = exited ]; then
    echo "rollback: STATE_A canonical stable restored (project=$PROJECT)"; echo "== OWNERSHIP_RESULT=ROLLBACK_OK =="; exit 1
  fi
  # STATE B — old-project stable still healthy (valid tier, explicitly classified)
  if [ "$ok" = 1 ] && [ "$img" = "$IMG" ] && [ -n "$OLD_PROJECT" ] && [ "$lbl" = "$OLD_PROJECT" ] \
     && [ "$h5056" = 200 ] && [ "$dns" = OK ] && [ "$par" = 200 ] && [ "$ttsr" = false ] && [ "$qstat" = exited ]; then
    echo "rollback: STATE_B old-project stable healthy (project=$OLD_PROJECT); re-own NOT completed"; echo "== OWNERSHIP_RESULT=ROLLBACK_OK =="; exit 1
  fi
  # STATE C — qual fallback, ONLY if stable is definitively absent OR definitively non-running (no-two-model, tri-state)
  local g; g=$(stable_gone)
  if [ "$g" = yes ] || { [ "$g" = no ] && [ "$(running voicebook-stream)" = false ]; }; then
    docker start voicebook-stream-qual >/dev/null 2>&1
    local qready=0; for i in $(seq 1 150); do [ "$(hc http://127.0.0.1:5060/healthz)" = 200 ] && { qready=1; break; }; sleep 1; done
    local qimg qrun par2 tts2
    qimg=$(docker inspect -f '{{.Image}}' voicebook-stream-qual 2>/dev/null); qrun=$(running voicebook-stream-qual)
    par2=$(hc "$PARAKEET"); tts2=$(running voicebook-tts)
    if [ "$qready" = 1 ] && [ "$qimg" = "$IMG" ] && [ "$qrun" = true ] && [ "$par2" = 200 ] && [ "$tts2" = false ]; then
      echo "rollback: STATE_C qual fallback serving 5060 (stable definitively gone/non-running)"; echo "== OWNERSHIP_RESULT=ROLLBACK_OK =="; exit 1
    fi
    echo "rollback: STATE_C attempted, qual not fully healthy"; echo "== OWNERSHIP_RESULT=ROLLBACK_FAILED =="; exit 2
  fi
  echo "rollback: stable present/unknown (state=$g running=$(running voicebook-stream) health=$(health voicebook-stream) project=$lbl), not a clean A/B; REFUSING qual start (no-two-model)"
  echo "== OWNERSHIP_RESULT=ROLLBACK_FAILED =="; exit 2
}
on_err() { [ "$PHASE" = preflight ] && { echo "aborted in preflight (no mutation)"; exit 1; }; rollback "trap: unexpected exit/signal in phase=$PHASE"; }

main() {
  echo "=== PREFLIGHT (fail-closed) ==="
  [ -n "$CANON_DIR" ] || die "usage: migrate-project-ownership.sh <durable CANON_DIR with pinned compose>"
  # BLOCKER 4: resolve the target PHYSICALLY; require an exact durable path, not a substring check
  local canon_abs staging_abs
  canon_abs=$(realpath "$CANON_DIR" 2>/dev/null) || die "CANON_DIR does not resolve: $CANON_DIR"
  [ -d "$canon_abs" ] || die "CANON_DIR is not a directory: $canon_abs"
  staging_abs=$(realpath "$STAGING_DIR" 2>/dev/null || echo "$STAGING_DIR")
  [ "$canon_abs" != "$staging_abs" ] || die "CANON_DIR resolves to the drill staging dir ($canon_abs) — refuse"
  case "$canon_abs" in *drill*) die "CANON_DIR resolved path contains a 'drill' component: $canon_abs";; esac
  echo "canonical home resolved: $canon_abs"
  COMPOSE="$canon_abs/docker-compose.stream.yaml"
  [ -f "$COMPOSE" ] || die "no compose at $COMPOSE"
  local sha; sha=$(shasum -a 256 "$COMPOSE" | awk '{print $1}')
  [ "$sha" = "$EXPECT_COMPOSE_SHA" ] || die "compose hash $sha != pinned $EXPECT_COMPOSE_SHA"
  local rendered; rendered=$(docker compose -f "$COMPOSE" config --format json 2>/dev/null | python3 -c 'import json,sys;print(json.load(sys.stdin).get("name",""))')
  [ "$rendered" = "$PROJECT" ] || die "canonical compose renders project '$rendered' != $PROJECT"
  # running service present, accepted digest, RUNNING and healthy, serving
  docker inspect voicebook-stream >/dev/null 2>&1 || die "voicebook-stream not present (nothing to re-own)"
  [ "$(running voicebook-stream)" = true ] || die "voicebook-stream not State.Running=true"   # BLOCKER 4
  [ "$(docker inspect -f '{{.Image}}' voicebook-stream)" = "$IMG" ] || die "running stable not accepted digest"
  [ "$(health voicebook-stream)" = healthy ] || die "running stable not healthy"
  [ "$(hc http://127.0.0.1:5056/healthz)" = 200 ] || die "host 5056 not 200"
  [ "$(dns_probe)" = OK ] || die "service DNS not reachable"
  OLD_PROJECT=$(proj_label voicebook-stream)
  [ -n "$OLD_PROJECT" ] || die "could not read current project label"
  [ "$OLD_PROJECT" != "$PROJECT" ] || { echo "already canonical (project=$PROJECT)"; echo "== OWNERSHIP_RESULT=ALREADY_CANONICAL =="; exit 0; }
  # rollback tier + peers intact
  [ "$(docker inspect -f '{{.State.Status}}' voicebook-stream-qual 2>/dev/null)" = exited ] || die "qual not stopped-intact"
  [ "$(docker inspect -f '{{.Image}}' voicebook-stream-qual 2>/dev/null)" = "$IMG" ] || die "qual not accepted digest"
  [ "$(running voicebook-tts)" = false ] || die "voicebook-tts not stopped"
  [ "$(hc "$PARAKEET")" = 200 ] || die "Parakeet not ready"
  echo "PREFLIGHT OK (stable running+healthy+accepted, OLD project=$OLD_PROJECT -> $PROJECT, qual stopped-intact, tts stopped, Parakeet 200)"

  echo "=== MIGRATE (prove exact target, remove old-project container, recreate canonical) ==="
  PHASE=migrate
  trap on_err ERR HUP INT TERM
  # BLOCKER 4: prove the down target is EXACTLY voicebook-stream under OLD_PROJECT, and canonical owns none
  local old_ps canon_ps
  old_ps=$(docker compose -p "$OLD_PROJECT" -f "$COMPOSE" ps --format '{{.Name}}' 2>/dev/null | sort | tr '\n' ',' )
  [ "$old_ps" = "voicebook-stream," ] || rollback "OLD project ps != exactly {voicebook-stream} (got '$old_ps')"
  canon_ps=$(docker compose -f "$COMPOSE" ps --format '{{.Name}}' 2>/dev/null | tr -d '[:space:]')
  [ -z "$canon_ps" ] || rollback "canonical project already owns containers ('$canon_ps') before migrate"
  docker compose -p "$OLD_PROJECT" -f "$COMPOSE" down >/dev/null 2>&1 || rollback "down of old project $OLD_PROJECT failed"
  local g; g=$(stable_gone)
  [ "$g" = yes ] || rollback "voicebook-stream not PROVEN gone after down (state=$g)"
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
  [ "$(running voicebook-tts)" = false ] || rollback "tts disturbed"

  echo "=== DONE ==="
  PHASE=done
  trap - ERR HUP INT TERM
  echo "re-owned: voicebook-stream project=$PROJECT (was $OLD_PROJECT), healthy, accepted digest, render OK, DNS OK, qual+tts undisturbed"
  echo "canonical lifecycle home: $canon_abs. Old staging dir may now be retired."
  echo "== OWNERSHIP_RESULT=SUCCESS =="
}

if [ "${BASH_SOURCE[0]}" = "${0}" ]; then main "$@"; fi
