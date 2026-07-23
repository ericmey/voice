# Slice 4 — Sumi LLM: Momo (qwen3.6-35b-a3b) via an explicit LiteLLM route — LANDED ✅

New behavior: Sumi's *mind* enters the loop. Her worker now reaches a local,
readable LLM through an explicit, constrained LiteLLM route — no cloud, no
double-speak, no hidden reasoning chain.

## Topology

- **Route:** a NEW explicit LiteLLM model alias, `sumi`, created 2026-07-23. It
  mirrors the proven `qwen/qwen3.6-35b-a3b-fast` backend contract but is its own
  route — the opaque existing `-fast` route was NOT mutated.
- **Backend:** `openai/qwen3.6-35b-a3b` at `http://momo.mey.house:8083/v1`,
  credential `Momo` (LiteLLM-stored; never handled in plaintext here).
- **Endpoint the worker uses:** `http://10.0.20.25:4000/v1` (mizuki's private IP),
  OpenAI-compatible. This is the endpoint proven reachable from `voice_default`
  (TCP + HTTP 200) — NOT the `litellm:4000` service DNS (NOREACH from the voice
  network) and NOT musubi's IP.
- **LiteLLM is DB-managed:** `config.yaml` has `model_list: []` with
  `store_model_in_db: true`, so all routes (including `-fast`) live in the
  database, not the file. The `sumi` route was created via the admin API
  (`POST /model/new`), read back via `GET /v1/model/info`.

## The route is deliberately CONSTRAINED

Three properties matter for a live phone turn, and all three are pinned:

1. **No-think.** The backend is a reasoning model. Left in thinking mode it emits
   `reasoning_content` with an **empty** `content` field (`finish=length`) —
   useless for speech. The `-fast`/`sumi` contract sets
   `chat_template_kwargs: {enable_thinking: false}`, which is the actual
   mechanism. Proven: `finish=stop`, `reasoning_content` length **0**, a real
   spoken `content` field.
2. **No duplicated spoken turn.** The route is `num_retries: 0`; the openai
   client is `max_retries=0`; **and** — the finding that matters — the livekit
   `AgentSession` has its OWN retry layer (`llm_conn_options`, default
   `max_retry=3` → 4 attempts) that re-runs the *whole* generation and can
   re-emit already-streamed tokens. So the worker pins
   `SessionConnectOptions(llm_conn_options=APIConnectOptions(max_retry=0))`:
   exactly one LLM attempt. Without this, the route-level `num_retries:0` is
   cosmetic. (STT/TTS keep the default 3 — their retries don't duplicate an LLM
   turn.)
3. **No cloud fallback.** The route has no fallback group. A Momo outage yields a
   hard error, never a silent escape to a cloud model. Proven fail-closed error:
   `InternalServerError: Connection error. No fallback model group found`. Sumi
   goes quiet before she speaks as something that isn't her.

## Proofs (2026-07-23, from the voice network / mizuki)

| Proof | Result |
|---|---|
| Route readback | `model=openai/qwen3.6-35b-a3b`, `api_base=momo:8083/v1`, `chat_template_kwargs.enable_thinking=false`, `num_retries=0`, `timeout=30`, cred `Momo` |
| Behavior | `finish=stop`, `content='Hi Eric, sending you a warm hello!'`, `reason_len=0`, 10 completion tokens |
| Latency (streaming) | **TTFT 0.321s / total 0.385s** — voice-grade first token |
| Fail-closed | dead backend → `InternalServerError: Connection error. No fallback model group found` — no completion returned, no cloud escape. Throwaway probe route deleted after. |

## Worker wiring (`agents/sumi/src/agent.py`)

- STT swap (Slice 3) stays; the LLM swap replaces the gemini scaffold:
  `openai_plugin.LLM(model="sumi", base_url="http://10.0.20.25:4000/v1",
  api_key=_llm_api_key(), temperature=0.7, max_retries=0, timeout=30)`. Model and
  base URL are env-overridable (`SUMI_LLM_MODEL` / `SUMI_LLM_BASE_URL`).
- **Fail-loud key.** `_llm_api_key()` reads `SUMI_LLM_API_KEY` (or
  `LITELLM_API_KEY`) and raises at startup if unset — Sumi's LLM has no cloud
  fallback and must not start on a default/empty key. Mirrors the persona's
  refuse-to-start stance.
- The gemini scaffold LLM and its `livekit.plugins.google` import are removed;
  only TTS (elevenlabs, Nyla's id) remains scaffold, for Slice 5.
- Gate: `ruff` clean, package imports, fail-loud verified both directions, 23/23
  unit tests pass.

## A note on where this was proven

The worker runs on mizuki / `voice_default`, where `http://10.0.20.25:4000` is
proven reachable and the route returns HTTP 200. The **command-chair mac** can
complete a TCP handshake to that endpoint but an actual HTTP request stalls
(asymmetric-path/firewall quirk) — so the plugin-object chat was NOT run from the
mac. That is not a Slice-4 defect: the mac is an editing surface, not a
deployment surface. The full plugin-object turn runs in the worker at Slice 7
(synthetic E2E), on the network where the endpoint is proven.

## Not yet up (expected, not broken)

The LiveKit plane is still OFF (nothing on 7880/7881/7882/5060). Remaining path:
Slice 5 voicebook-stream TTS adapter → isolated Sumi worker → LiveKit/SIP →
synthetic turn (Slice 7) → the real call (Slice 8).
