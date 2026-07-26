# Qwen phone-agent POC — measured results

**Box:** mizuki, 2× RTX 5060 Ti **16 GB** (Blackwell sm_120), driver 595.84 / CUDA 13.2.
**Lane:** GPU 1 only. GPU 0 carries STT/TTS (voicebook-stream + Parakeet) and is not ours.
**Production target:** Google G2 **L4, 24 GB**.

> **The POC box has ~2/3 the KV headroom of production.** Every context and
> concurrency number below is a **floor** for the L4, not a forecast of it.

**Harness:** `scripts/eval-call-agent.py` + `scripts/cases/phone-agent.json`.
12 call-shaped cases — tool selection, spoken-number normalisation, multi-arg
extraction, mid-turn interruption, post-tool-result phrasing, and instruction
adherence under pressure. Speaks OpenAI `/v1/chat/completions`, so identical
cases run against llama.cpp, vLLM, and the L4.

---

## 1. Thinking mode was the product bug

Qwen3.5's chat template defaults **thinking ON**. llama.cpp streams it in a
separate `reasoning_content` field. **The caller hears none of it and waits
through all of it.**

Same server, same cases, same seed. Only `enable_thinking` changed:

| | thinking ON | thinking OFF |
|---|---|---|
| pass | 7/12 (58%) | **10/12 (83%)** |
| TTFT p50 | 3.50 s | **0.178 s** |
| TTFT p95 | 3.91 s | 0.30 s |
| decode | 26.9 tok/s | **67.7 tok/s** |

**~20× on TTFT.** Five of the twelve spent their entire 256-token budget
thinking and produced **zero audible output** (`finish_reason: length`).

**3.5 seconds of silence before the first word is not a phone agent.**

Pinned **server-side** as `--reasoning off`, not left to a per-request
`chat_template_kwargs`. A caller that forgets the kwarg gets the dead air, and
callers forget. (The Sumi LiteLLM route already pins it for the same reason —
two independent discoveries of one invariant.)

## 2. `--parallel 2` was the concurrency ceiling, not the model

Old config, thinking off:

| concurrency | pass | TTFT p50 | decode p50 |
|---|---|---|---|
| 1 | 10/12 | 0.18 s | 67.7 |
| 2 | 11/12 | 0.23 s | 45.3 |
| 4 | 11/12 | 1.03 s | 45.8 |
| 8 | 10/12 | 2.57 s | 48.4 |

**Decode barely moves; TTFT explodes.** That is a queue, not saturation —
`--parallel 2` meant callers 3–8 were waiting in line. Confirmed at the source:
the load log reports `new slot, n_ctx = 8192` for each of two slots, i.e.
**`--ctx-size` is split across slots**, against a model whose `n_ctx_train` is
**262144**.

## 3. The qualified profile, and the ceiling found by crossing it

| slots × ctx | VRAM | c=8 | c=16 | c=24 | result |
|---|---|---|---|---|---|
| 2 × 8192 | 5.9 GB | 2.57 s | — | — | old |
| 16 × 16384 | 10.7 GB | 0.72 s | 0.88 s | — | ok |
| **24 × 16384** | **13.3 GB** | **0.54 s** | 0.96 s | **0.83 s** | **QUALIFIED** |
| 32 × 16384 | 15.8 GB | — | **ABORT** | — | **measured failure** |

At **24 × 16384**: 24 concurrent callers, **12/12 pass**, TTFT p50 **0.83 s**,
~22 tok/s per stream, `RestartCount=0`.

**32 slots loads and reports `healthy`** at 15836/16311 MiB — and then aborts
under real concurrent load, because ~475 MiB is not enough for the compute
buffers. `restart: unless-stopped` recovered it automatically.

> **A container that reports healthy is not a server that works.** The health
> check passed on a configuration that could not survive its first real load.
> The watchdog is proven here by an actual crash, not by assertion.

### Per-stream decode is priced against speech, not against a benchmark

Human speech is ~3–4 tokens/sec. **22 tok/s per stream is ~6× real-time** — the
model finishes each sentence long before TTS can say it. Chasing maximal decode
optimises a number nobody hears. Staying ahead of the mouth is the target.

### Warm-up artifact — discard the first sweep after a restart

The first post-restart sweep at c=8 read **1.47 s** and settled to **0.54 s /
0.57 s** on two reruns. Cold prompt cache. It was reported as unexplained and
then resolved by rerunning, rather than promoted into a cache story on first
sight.

---

## Instrument corrections

The harness was wrong twice before it could be trusted, **both times in the
flattering direction** — which is the direction that gets believed:

1. **Reported 75% pass** by grading the five silent non-replies as ordinary
   instruction failures ("missing required text"). That reads as *the model is
   a bit sloppy* rather than *the model never spoke.* A reply that never existed
   cannot be graded on its wording. `thinking_overrun` is now its own failure
   shape.
2. **Reported decode at 26.9 tok/s** when the server log said **68.75** — it
   counted only audible tokens against total wall time, including the thinking
   window. Reasoning tokens are now counted separately and never folded in.

**Failure shape is never collapsed into pass rate.** A silent `bad_args` — right
tool, wrong arguments — is far worse than a visible refusal, because every layer
above the model reads it as success.

---

## Open

- Abliterated `Huihui-Qwen3.5-9B-abliterated.Q4_K_M` vs base, same config, so
  any delta is attributable to abliteration alone.
- vLLM comparison for the L4 serving decision (continuous batching + paged KV).
- Grafana: `--metrics` is live on the serving port; wiring is best-effort and
  explicitly not an exporter project.
