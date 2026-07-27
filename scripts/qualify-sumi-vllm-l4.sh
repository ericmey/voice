#!/usr/bin/env bash
# Qualify exactly one observed concurrency point on the production L4 target.
#
# Required environment:
#   SUMI_VLLM_API_KEY, SUMI_VLLM_MODEL_REVISION, SUMI_VLLM_QUANTIZATION
#
# The API key is never placed in the shell command line. The existing Python
# harness only accepts --api-key, so this wrapper injects it into sys.argv after
# process start via runpy; it is absent from the OS process listing.
set -euo pipefail
umask 077

usage() {
  echo "usage: $0 CONCURRENCY [ARTIFACT_DIR]" >&2
  exit 2
}

[[ $# -ge 1 && $# -le 2 ]] || usage
[[ "$1" =~ ^[1-9][0-9]*$ ]] || usage

CONCURRENCY="$1"
REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
STAMP="$(date -u +%Y-%m-%dT%H-%M-%SZ)"
ARTIFACT_DIR="${2:-${REPO_ROOT}/artifacts/l4-vllm/${STAMP}-c${CONCURRENCY}}"
BASE_URL="${SUMI_VLLM_BASE_URL:-http://127.0.0.1:8088/v1}"
MODEL="${SUMI_VLLM_SERVED_MODEL:-qwen3.5-9b}"
MAX_TTFT="${SUMI_VLLM_MAX_TTFT_P95:-1.5}"
MIN_PASS_RATE="${SUMI_VLLM_MIN_PASS_RATE:-0.85}"
MIN_DECODE_TPS="${SUMI_VLLM_MIN_DECODE_TPS:-8.0}"
CONTAINER="${SUMI_VLLM_CONTAINER:-sumi-vllm-l4}"

: "${SUMI_VLLM_API_KEY:?SUMI_VLLM_API_KEY is required}"
: "${SUMI_VLLM_MODEL_REVISION:?SUMI_VLLM_MODEL_REVISION is required}"
: "${SUMI_VLLM_QUANTIZATION:?SUMI_VLLM_QUANTIZATION is required}"

mkdir -p "$ARTIFACT_DIR"
ARTIFACT_DIR="$(cd "$ARTIFACT_DIR" && pwd)"
chmod 700 "$ARTIFACT_DIR"

for command in docker python3 shasum nvidia-smi; do
  command -v "$command" >/dev/null || {
    echo "qualify-sumi-vllm-l4: missing required command: $command" >&2
    exit 2
  }
done

container_receipt() {
  docker inspect --format \
    '{"name":{{json .Name}},"image":{{json .Image}},"status":{{json .State.Status}},"health":{{json (or .State.Health.Status "none")}},"restarts":{{.RestartCount}},"started_at":{{json .State.StartedAt}},"model_revision":{{json (index .Config.Labels "ai.harem.model.revision")}},"quantization":{{json (index .Config.Labels "ai.harem.model.quantization")}}}' \
    "$CONTAINER"
}

api_get() {
  local path="$1"
  local output="$2"
  SUMI_PROBE_PATH="$path" SUMI_PROBE_OUTPUT="$output" python3 - <<'PY'
import os
import urllib.request

url = os.environ["SUMI_VLLM_BASE_URL"].rstrip("/") + os.environ["SUMI_PROBE_PATH"]
request = urllib.request.Request(
    url,
    headers={"Authorization": f"Bearer {os.environ['SUMI_VLLM_API_KEY']}"},
)
with urllib.request.urlopen(request, timeout=30) as response:
    body = response.read()
with open(os.environ["SUMI_PROBE_OUTPUT"], "wb") as handle:
    handle.write(body)
PY
}

export SUMI_VLLM_BASE_URL="$BASE_URL"

echo "qualify-sumi-vllm-l4: capturing pre-run state"
container_receipt >"$ARTIFACT_DIR/container-before.json"
nvidia-smi --query-gpu=timestamp,name,uuid,driver_version,memory.total,memory.used,memory.free,utilization.gpu \
  --format=csv,noheader >"$ARTIFACT_DIR/nvidia-before.csv"
api_get "/models" "$ARTIFACT_DIR/models.json"
# /metrics is rooted outside /v1.
SUMI_VLLM_BASE_URL="${BASE_URL%/v1}" api_get "/metrics" "$ARTIFACT_DIR/metrics-before.txt"

python3 "$REPO_ROOT/scripts/probe-openai-stream-contract.py" \
  --base-url "$BASE_URL" \
  --model "$MODEL" \
  --max-ttft "$MAX_TTFT" \
  --out "$ARTIFACT_DIR/protocol.json" \
  | tee "$ARTIFACT_DIR/protocol.log"

case_count() {
  python3 - "$1" <<'PY'
import json
import sys
print(len(json.load(open(sys.argv[1]))))
PY
}

run_eval() {
  local label="$1"
  local cases="$2"
  local count repeat output log rc
  count="$(case_count "$cases")"
  repeat=$(( (CONCURRENCY + count - 1) / count ))
  output="$ARTIFACT_DIR/${label}.json"
  log="$ARTIFACT_DIR/${label}.log"

  export SUMI_EVAL_SCRIPT="$REPO_ROOT/scripts/eval-call-agent.py"
  export SUMI_EVAL_CASES="$cases"
  export SUMI_EVAL_OUT="$output"
  export SUMI_EVAL_REPEAT="$repeat"
  export SUMI_EVAL_CONCURRENCY="$CONCURRENCY"
  export SUMI_EVAL_MODEL="$MODEL"

  set +e
  python3 -c '
import os, runpy, sys
sys.argv = [
    os.environ["SUMI_EVAL_SCRIPT"],
    "--base-url", os.environ["SUMI_VLLM_BASE_URL"],
    "--model", os.environ["SUMI_EVAL_MODEL"],
    "--cases", os.environ["SUMI_EVAL_CASES"],
    "--concurrency", os.environ["SUMI_EVAL_CONCURRENCY"],
    "--repeat", os.environ["SUMI_EVAL_REPEAT"],
    "--api-key", os.environ["SUMI_VLLM_API_KEY"],
    "--no-think",
    "--out", os.environ["SUMI_EVAL_OUT"],
]
runpy.run_path(os.environ["SUMI_EVAL_SCRIPT"], run_name="__main__")
' 2>&1 | tee "$log"
  rc="${PIPESTATUS[0]}"
  set -e
  echo "$rc" >"$ARTIFACT_DIR/${label}.harness-exit"
  [[ -s "$output" ]] || {
    echo "qualify-sumi-vllm-l4: $label produced no run JSON" >&2
    return 1
  }
}

# Harness exit 1 means at least one behavioral case failed. Qualification uses
# the explicit pass-rate and failure-shape gates below, so retain that receipt
# rather than letting shell errexit discard it.
run_eval phone "$REPO_ROOT/scripts/cases/phone-agent.json"
run_eval customer-service "$REPO_ROOT/scripts/cases/customer-service.json"

container_receipt >"$ARTIFACT_DIR/container-after.json"
nvidia-smi --query-gpu=timestamp,name,uuid,driver_version,memory.total,memory.used,memory.free,utilization.gpu \
  --format=csv,noheader >"$ARTIFACT_DIR/nvidia-after.csv"
SUMI_VLLM_BASE_URL="${BASE_URL%/v1}" api_get "/metrics" "$ARTIFACT_DIR/metrics-after.txt"

export SUMI_QUAL_ARTIFACT_DIR="$ARTIFACT_DIR"
export SUMI_QUAL_CONCURRENCY="$CONCURRENCY"
export SUMI_QUAL_MAX_TTFT="$MAX_TTFT"
export SUMI_QUAL_MIN_PASS_RATE="$MIN_PASS_RATE"
export SUMI_QUAL_MIN_DECODE_TPS="$MIN_DECODE_TPS"
export SUMI_QUAL_MODEL_REVISION="$SUMI_VLLM_MODEL_REVISION"
export SUMI_QUAL_QUANTIZATION="$SUMI_VLLM_QUANTIZATION"

set +e
python3 - <<'PY'
import json
import os
import sys
from pathlib import Path

root = Path(os.environ["SUMI_QUAL_ARTIFACT_DIR"])
requested = int(os.environ["SUMI_QUAL_CONCURRENCY"])
max_ttft = float(os.environ["SUMI_QUAL_MAX_TTFT"])
min_pass = float(os.environ["SUMI_QUAL_MIN_PASS_RATE"])
min_decode = float(os.environ["SUMI_QUAL_MIN_DECODE_TPS"])
dangerous = {
    "bad_args",
    "empty",
    "malformed",
    "malformed_tool_args",
    "no_tool_call",
    "thinking_overrun",
    "transport",
    "wrong_tool",
}
before = json.loads((root / "container-before.json").read_text())
after = json.loads((root / "container-after.json").read_text())
checks = []

def check(name, ok, detail):
    checks.append({"name": name, "ok": bool(ok), "detail": detail})

check(
    "restart_count_unchanged",
    before["restarts"] == after["restarts"],
    f"{before['restarts']} -> {after['restarts']}",
)
check("container_healthy", after["status"] == "running" and after["health"] == "healthy", str(after))

runs = {}
for label in ("phone", "customer-service"):
    report = json.loads((root / f"{label}.json").read_text())
    runs[label] = report
    meta = report["meta"]
    latency = report["latency"]
    reasoning = report.get("reasoning") or {}
    shapes = set(report.get("failure_shapes") or {})
    check(
        f"{label}.observed_concurrency",
        meta.get("concurrency_honest") is True
        and meta.get("peak_in_flight", 0) >= requested
        and meta.get("wave_broken") is not True,
        (
            f"requested={requested} observed={meta.get('peak_in_flight')} "
            f"wave_broken={meta.get('wave_broken')}"
        ),
    )
    check(
        f"{label}.pass_rate",
        report["totals"]["pass_rate"] >= min_pass,
        f"{report['totals']['pass_rate']:.4f} >= {min_pass:.4f}",
    )
    check(
        f"{label}.ttft_p95",
        latency.get("ttft_p95") is not None and latency["ttft_p95"] <= max_ttft,
        f"{latency.get('ttft_p95')}s <= {max_ttft}s",
    )
    check(
        f"{label}.decode_p50",
        latency.get("decode_tps_p50") is not None and latency["decode_tps_p50"] >= min_decode,
        f"{latency.get('decode_tps_p50')} >= {min_decode} tok/s",
    )
    check(
        f"{label}.no_reasoning",
        reasoning.get("reasoning_tokens_total", 0) == 0,
        f"reasoning_tokens={reasoning.get('reasoning_tokens_total', 0)}",
    )
    found = sorted(shapes & dangerous)
    check(f"{label}.no_dangerous_failure", not found, f"found={found}")

protocol = json.loads((root / "protocol.json").read_text())
check("protocol_contract", protocol.get("ok") is True, str(protocol.get("failure_shape", "ok")))
receipt = {
    "accepted": all(item["ok"] for item in checks),
    "scope": "one synchronized first-wave concurrency point",
    "requested_concurrency": requested,
    "observed_peak": {label: report["meta"].get("peak_in_flight") for label, report in runs.items()},
    "wave_broken": {label: report["meta"].get("wave_broken") for label, report in runs.items()},
    "model_revision": os.environ["SUMI_QUAL_MODEL_REVISION"],
    "quantization": os.environ["SUMI_QUAL_QUANTIZATION"],
    "thresholds": {
        "ttft_p95_s_max": max_ttft,
        "pass_rate_min": min_pass,
        "decode_tps_p50_min": min_decode,
    },
    "checks": checks,
}
(root / "qualification.json").write_text(json.dumps(receipt, indent=2) + "\n")
for item in checks:
    mark = "PASS" if item["ok"] else "FAIL"
    print(f"{mark:4} {item['name']}: {item['detail']}")
print(f"\nqualification: {'ACCEPTED' if receipt['accepted'] else 'REJECTED'} at observed c={requested}")
sys.exit(0 if receipt["accepted"] else 1)
PY
QUALIFICATION_RC="$?"
set -e

git -C "$REPO_ROOT" rev-parse HEAD >"$ARTIFACT_DIR/voice-git-head.txt"
SUMS_TMP="${ARTIFACT_DIR}.SHA256SUMS.$$"
(
  cd "$ARTIFACT_DIR"
  find . -type f ! -name SHA256SUMS -print0 \
    | sort -z \
    | xargs -0 shasum -a 256 >"$SUMS_TMP"
)
mv "$SUMS_TMP" "$ARTIFACT_DIR/SHA256SUMS"

echo "qualify-sumi-vllm-l4: receipt $ARTIFACT_DIR/qualification.json"
echo "qualify-sumi-vllm-l4: hashes  $ARTIFACT_DIR/SHA256SUMS"
exit "$QUALIFICATION_RC"
