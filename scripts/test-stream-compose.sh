#!/usr/bin/env bash
# Rendered-config assertions for docker-compose.stream.yaml (Slice 1 artifact).
# Renders the compose and asserts the frozen Slice-1 contract invariants.
set -uo pipefail
cd "$(dirname "$0")/.."
R=$(docker compose -f docker-compose.stream.yaml config 2>&1) || { echo "FAIL: compose config did not render"; echo "$R"; exit 2; }
fail=0
check() { if echo "$R" | grep -qE "$1"; then echo "OK   $2"; else echo "FAIL $2"; fail=1; fi; }
check 'image: voicebook-stream@sha256:3b28aa8102d69b3214687a7e732dcdeca35b8a11ab0d34187e1dad3f9b4472f7' "image pinned by immutable digest"
check 'pull_policy: never'          "pull disabled"
check 'published: "5056"'           "host ops publish 5056"
check 'target: 5060'                "container port 5060"
check 'voice_default'               "attached to voice_default"
check 'external: true'              "voice_default/volume external (join, not recreate)"
check '/srv/voicebook:ro'           "masters read-only"
check 'registry.json:ro'            "registry read-only"
check '/models/hf-cache:ro'         "model cache read-only"
check 'HF_HUB_OFFLINE'              "offline env"
check 'restart: unless-stopped'     "restart policy"
check 'urllib.request.urlopen'      "python-stdlib healthcheck (no curl/wget dependency)"
if echo "$R" | grep -iqE 'api_key|secret|gemini|openai|elevenlabs|password|momo_api'; then echo "FAIL no-secrets"; fail=1; else echo "OK   no secrets/cloud in rendered config"; fi
[ "$fail" = 0 ] && echo "== STREAM_COMPOSE_TEST=PASS ==" || echo "== STREAM_COMPOSE_TEST=FAIL =="
exit "$fail"
