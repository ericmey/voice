#!/usr/bin/env bash
# Renders docker-compose.stream.yaml to JSON and runs structured assertions
# (scripts/assert_stream_compose.py). RED-PROOFS the security boundary: a
# 0.0.0.0 host bind and a writable masters mount MUST fail the assertions.
set -uo pipefail
cd "$(dirname "$0")/.."
render() { docker compose -f "$1" config --format json 2>/dev/null; }

echo "--- structured assertions (real compose, must PASS) ---"
render docker-compose.stream.yaml > /tmp/stream.json || { echo "FAIL: compose did not render"; exit 2; }
python3 scripts/assert_stream_compose.py /tmp/stream.json
rc=$?

rp() { # $1 sed-expr  $2 label  $3 expect-reject-msg
  local tmp="/tmp/stream-rp-$$.yaml"
  sed "$1" docker-compose.stream.yaml > "$tmp"
  render "$tmp" > /tmp/stream-rp.json 2>/dev/null
  if python3 scripts/assert_stream_compose.py /tmp/stream-rp.json >/dev/null 2>&1; then
    echo "RED-PROOF $2: FAIL — mutation was NOT rejected ($3)"; rc=1
  else
    echo "RED-PROOF $2: OK — $3 correctly rejected"
  fi
  rm -f "$tmp"
}

echo "--- red-proofs (mutations MUST be rejected) ---"
rp 's/127.0.0.1:5056:5060/0.0.0.0:5056:5060/'   host_ip   "0.0.0.0 exposure"
rp 's#:/srv/voicebook:ro#:/srv/voicebook:rw#'    read_only "writable masters mount"

[ "$rc" = 0 ] && echo "== SLICE1_TEST=PASS ==" || echo "== SLICE1_TEST=FAIL =="
exit "$rc"
