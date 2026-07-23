#!/usr/bin/env bash
# Renders docker-compose.stream.yaml to JSON and runs structured assertions
# (scripts/assert_stream_compose.py). RED-PROOFS the security boundary: a
# 0.0.0.0 host bind, a writable masters mount, an injected env key, and an
# injected service+top-level secret MUST ALL fail the assertions.
set -uo pipefail
cd "$(dirname "$0")/.."
render() { docker compose -f "$1" config --format json 2>/dev/null; }
passes() { python3 scripts/assert_stream_compose.py "$1" >/dev/null 2>&1; }
rc=0

echo "--- structured assertions (real compose, must PASS) ---"
render docker-compose.stream.yaml > /tmp/stream.json || { echo "FAIL: compose did not render"; exit 2; }
python3 scripts/assert_stream_compose.py /tmp/stream.json || rc=1

echo "--- red-proofs (each mutation MUST be rejected) ---"
redproof() { # $1 mutated-compose-file  $2 label  $3 desc
  render "$1" > /tmp/rp.json 2>/dev/null
  if passes /tmp/rp.json; then echo "RED-PROOF $2: FAIL — $3 was NOT rejected"; rc=1
  else echo "RED-PROOF $2: OK — $3 correctly rejected"; fi
}

T=/tmp/rp-$$.yaml
sed 's/127.0.0.1:5056:5060/0.0.0.0:5056:5060/' docker-compose.stream.yaml > "$T"; redproof "$T" host_ip   "0.0.0.0 exposure"
sed 's#:/srv/voicebook:ro#:/srv/voicebook:rw#'  docker-compose.stream.yaml > "$T"; redproof "$T" read_only "writable masters mount"

python3 - > "$T" <<'PY'
s = open('docker-compose.stream.yaml').read()
s = s.replace("      - VOICEBOOK_PORT=5060\n",
              "      - VOICEBOOK_PORT=5060\n      - INJECTED_TOKEN=leaked\n", 1)
print(s)
PY
redproof "$T" env_key "injected env key"

python3 - > "$T" <<'PY'
s = open('docker-compose.stream.yaml').read()
s = s.replace("    container_name: voicebook-stream\n",
              "    container_name: voicebook-stream\n    secrets:\n      - k\n", 1)
s += "\nsecrets:\n  k:\n    file: /etc/hostname\n"
print(s)
PY
redproof "$T" secrets "injected service+top-level secret"
rm -f "$T" /tmp/rp.json

[ "$rc" = 0 ] && echo "== SLICE1_TEST=PASS ==" || echo "== SLICE1_TEST=FAIL =="
exit "$rc"
