# Qwen phone-agent qualification on GCP G2 / NVIDIA L4

This package qualifies a Qwen 9B phone-agent candidate on the actual production
GPU architecture. It does not infer L4 behavior from Mizuki's RTX 5060 Ti, and
it does not launch paid capacity. An operator supplies the instance, the model
export, and the explicit concurrency point to test.

The package has four parts:

- `deploy/sumi-vllm-l4/docker-compose.sumi-vllm-l4.yaml` — pinned vLLM service;
- `scripts/probe-openai-stream-contract.py` — streamed protocol canary;
- `scripts/qualify-sumi-vllm-l4.sh` — one-point acceptance runner; and
- the existing `scripts/eval-call-agent.py` plus both case sets.

## Why vLLM on the L4

The production target is an NVIDIA L4 (Ada, compute capability 8.9), while
Mizuki's RTX 5060 Ti is Blackwell (12.0). vLLM qualification belongs on the L4:
testing on Mizuki would mix the serving question with a different kernel and
build path. llama.cpp remains the qualified local Mizuki server.

vLLM is the production-shaped choice because its scheduler, continuous batching,
and paged KV cache are designed for concurrent serving. The image is pinned to
the linux/amd64 manifest of v0.23.0. The service uses the Qwen3 XML tool parser,
Qwen3 reasoning parser, and server-side `enable_thinking=false`. Qwen3.5 is
served with `--language-model-only` so multimodal state does not consume memory
needed by the phone agent's KV cache.

Primary references:

- [vLLM Docker deployment](https://docs.vllm.ai/en/v0.23.0/deployment/docker/)
- [vLLM serve arguments](https://docs.vllm.ai/en/v0.23.0/cli/serve/)
- [Tool calling](https://docs.vllm.ai/en/v0.23.0/features/tool_calling/)
- [Supported Qwen3.5 models](https://docs.vllm.ai/en/v0.23.0/models/supported_models/)

## Before launch

The mounted model directory must already exist on the L4 host. Do not download a
floating repository branch during service startup. Record the immutable source
revision and the export's quantization explicitly:

```bash
export SUMI_VLLM_MODEL_DIR=/srv/models/qwen-phone-agent-candidate
export SUMI_VLLM_MODEL_REVISION=<immutable-huggingface-or-training-revision>
export SUMI_VLLM_QUANTIZATION=<the-export-quantization>
export SUMI_VLLM_API_KEY=<generated-local-service-key>
export SUMI_VLLM_GPU_ID=0
```

The compose file defaults to 16K context, a 32-sequence scheduler ceiling, and
0.92 GPU memory utilization. These are launch parameters, not qualified capacity.
The candidate export format and quantization remain an explicit pre-launch
decision because next week's finetune artifact does not exist yet.

Validate the rendered service before starting it:

```bash
docker compose \
  -f deploy/sumi-vllm-l4/docker-compose.sumi-vllm-l4.yaml \
  config --quiet

docker compose \
  -f deploy/sumi-vllm-l4/docker-compose.sumi-vllm-l4.yaml \
  up -d
```

The port is bound to `127.0.0.1:8088`. Production exposure belongs behind the
deployment's authenticated network or proxy, not on an open vLLM port.

## Protocol preflight

Run the canary before a behavioral or capacity test:

```bash
SUMI_VLLM_API_KEY="$SUMI_VLLM_API_KEY" \
  python3 scripts/probe-openai-stream-contract.py \
    --base-url http://127.0.0.1:8088/v1 \
    --model qwen3.5-9b \
    --max-ttft 1.5 \
    --out /tmp/vllm-protocol.json
```

It rejects all of these independently:

- non-empty `reasoning_content` or `reasoning` fields;
- `<think>` markup in audible output;
- no audible reply or TTFT above the voice bound;
- tool deltas without integer `index` values;
- fragments that do not reassemble into `lookup_order`; and
- an argument other than order `44821`.

Accepting `chat_template_kwargs` is not proof that thinking is off. The canary
checks the response. This protects against a server that politely ignores the
field and adds seconds of silent reasoning to every phone turn.

## Qualify one observed point

Choose exactly one concurrency value per invocation:

```bash
export SUMI_VLLM_MODEL_REVISION SUMI_VLLM_QUANTIZATION SUMI_VLLM_API_KEY
scripts/qualify-sumi-vllm-l4.sh 16
```

The runner:

1. captures a narrow container receipt, GPU state, `/v1/models`, and `/metrics`;
2. runs the protocol preflight;
3. calculates enough repeats for both case sets to create the requested work;
4. runs tool/instruction and customer-service cases;
5. requires the harness's observed `peak_in_flight` to reach the requested value;
6. rejects a broken first wave even if the requested peak is reached later;
7. rejects leaked reasoning and dangerous failure shapes;
8. applies explicit pass-rate, TTFT p95, and decode thresholds;
9. requires the container restart count to remain unchanged; and
10. writes a private, hashed artifact bundle.

Defaults are:

```text
TTFT p95                 <= 1.5 seconds
pass rate                >= 0.85
decode p50               >= 8 tokens/second/stream
dangerous failure shapes == none
reasoning tokens         == 0
restart count change     == 0
```

Override thresholds only before a run, and record why:

```bash
SUMI_VLLM_MAX_TTFT_P95=1.25 \
SUMI_VLLM_MIN_PASS_RATE=0.90 \
SUMI_VLLM_MIN_DECODE_TPS=8 \
  scripts/qualify-sumi-vllm-l4.sh 16
```

The API key is read from the environment. It is not placed in shell arguments,
receipts, full `docker inspect` output, or hashes.

## Capacity claims

A run may claim only the concurrency recorded as observed in its own receipt.
The runner qualifies synchronized first-wave load. It does not measure staggered
arrival, calls per hour, or steady-state arrival/departure behavior.

To map candidate points, run each separately:

```bash
scripts/qualify-sumi-vllm-l4.sh 16
scripts/qualify-sumi-vllm-l4.sh 20
scripts/qualify-sumi-vllm-l4.sh 24
```

The highest passing tested point is a conservative operating point. It is not an
exact maximum, and no result may be interpolated across unmeasured values. A
later staggered-arrival test is a separate instrument and must be labelled as
such.

The local Mizuki result must not be used as an L4 forecast. Its honest scope is:
16 synchronized requests qualified at TTFT p95 1.36 seconds; 24 was rejected at
5.24 seconds; 17 through 23 remain unmeasured.

## Rollback and failure handling

Keep the last accepted model directory mounted separately. Rollback changes the
read-only model mount back to that known revision, recreates the container, and
reruns protocol plus behavioral acceptance. Do not call a successful restart a
rollback proof.

```bash
docker compose \
  -f deploy/sumi-vllm-l4/docker-compose.sumi-vllm-l4.yaml \
  down
```

Do not run old and new servers on the same host port. A health check proves the
process answers `/health`; it does not prove thinking is disabled, tool fragments
assemble, latency holds, or the model follows the phone contract.

If qualification fails, retain the full artifact directory. Do not rerun blind
after a restart-count change, tool-contract failure, or `thinking_not_disabled`.
Those failures require inspection before another candidate is promoted.

`/metrics` is captured in every receipt. It is not yet scraped into Grafana on
this fleet; adding a Prometheus scrape and remote-write path is a separate piece
of infrastructure, not part of this qualification.
