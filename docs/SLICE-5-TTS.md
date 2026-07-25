# Slice 5 — Sumi TTS: her own voice via voicebook-stream (custom LiveKit plugin) — LANDED ✅

New behavior: Sumi speaks in **her own accepted master voice**, locally. The last
inherited cloud scaffold (ElevenLabs on Nyla's id) is gone — the pipeline is now
fully local end to end. The original landing topology was Parakeet STT → Momo
LLM → voicebook-stream TTS; the accepted 2026-07-25 phone topology is Faster
Whisper STT → local Qwen3.5-9B → voicebook-stream TTS.

## Topology

- **Adapter:** `agents/sumi/src/voicebook_tts.py` — a custom `livekit.agents.tts.TTS`
  (`VoicebookTTS`) that drives the managed voicebook-stream service.
- **Endpoint:** `POST http://voicebook-stream:5060/speak/stream` (service DNS on
  `voice_default`), body `{"voice_id":"sumi-v1","text":...}` → raw **s16le PCM,
  24000 Hz mono** (`X-Audio-Format: s16le`, `X-Sample-Rate: 24000`). Voice id and
  base URL are env-overridable (`SUMI_TTS_VOICE_ID` / `SUMI_TTS_BASE_URL`).
- **Voice:** `sumi-v1` — her frozen entry in the server-owned registry (alongside
  `nyla-v1`). An unknown id fails loud (404 → APIStatusError); she never speaks in
  a substitute voice.

## Contract decisions baked into the adapter

- **Full-text input, streaming output.** `capabilities.streaming=False` — the LLM
  produces a sentence, the voice pipeline's StreamAdapter chunks it, and each
  chunk is one `/speak/stream` request that streams PCM back. `synthesize()` →
  `ChunkedStream._run(output_emitter)` → `initialize(mime_type="audio/pcm",
  sample_rate=24000, num_channels=1)` → push chunks → flush.
- **One-flight lease is fine.** The service serializes synthesis (429 if a second
  request overlaps). A single Sumi call never overlaps itself; a 429 is a
  transient, retryable state.
- **Cancellation is free.** LiveKit cancels the `_run` task on barge-in; aiohttp
  closes the connection; the server observes the disconnect and releases the lease
  (voicebook-stream T6 qual). No explicit abort handshake needed.
- **TTS retries do NOT double-speak** — so, unlike the LLM (pinned to
  `max_retry=0`), the default TTS retry is left ON. The TTS base `_main_task`
  calls `output_emitter.aclose()` to DISCARD a failed attempt's audio before
  retrying under a fresh request id; the LLM layer, by contrast, re-emits already-
  streamed tokens. Different layers, different correct choice.
- **Verify the instrument.** A 200 whose `X-Audio-Format` is not `s16le` is
  rejected (raises), never played as noise. HTTP errors map to `APIStatusError`
  (status preserved), never a silent empty turn.
- **No secret handled.** voicebook-stream is internal with no api-key on the
  stream path; `voice_id` selects the frozen voice server-side.

## Proofs (2026-07-23)

**Live raw contract** (mizuki → voicebook-stream, the exact request the adapter sends):
- `POST /speak/stream {sumi-v1, "Good evening, Eric. I am here. How are you?"}` →
  HTTP 200, `X-Audio-Format: s16le`, `X-Sample-Rate: 24000`,
  `Content-Type: application/octet-stream`.
- 130560 bytes (even), 65280 samples, **2.72 s**, peak 17151, **rms 2744 (non-silent
  — real speech)**.

**Live adapter-object seam** (`VoicebookTTS.synthesize()` through the real LiveKit
`AudioEmitter`, over an SSH tunnel so the dev venv reaches the service):
- 20 frames, **TTFA 0.254 s**, total 1.750 s for 2.97 s of audio → **RTF 0.589**
  (faster than realtime — will not stall the call).
- `sample_rate=24000`, `num_channels=1`, 71280 samples — 24 kHz mono through the
  emitter, exactly the AgentSession's expectation.

**CI unit tests** (`tests/test_voicebook_tts.py`, 6, no live service): request
shape `{voice_id, text}` to `/speak/stream`; PCM→frames at 24 kHz mono; empty
voice_id fails loud; HTTP 429 → `APIStatusError(status_code=429)` (not silent);
non-s16le 200 rejected. Full sumi suite: **29 passed**, ruff clean.

## Worker wiring (`agents/sumi/src/agent.py`)

`tts = VoicebookTTS(voice_id="sumi-v1", base_url="http://voicebook-stream:5060")`
replaces the elevenlabs scaffold; the `livekit.plugins.elevenlabs` import and the
`_ELEVENLABS_*` ids are removed. The module docstring and `on_enter` note now
describe a fully-local pipeline and a deterministic spoken opener.

## Landing-time status (historical, 2026-07-23)

At the time this slice first landed, the LiveKit plane was still off. All three pipeline
components were wired and individually proven. The remaining path then was: **isolated Sumi
worker → LiveKit/SIP bring-up (Slice 6) → single-client synthetic E2E (Slice 7) →
Eric's call (Slice 8)**. Those later slices are now live; this paragraph is retained
only as the landing record.

## 2026-07-25 phone-artifact qualification

The current phone path is operational and its latency dropouts were removed by
the local Qwen3.5-9B route plus the longer endpointing settle window. A separate,
intermittent within-utterance warble remains audible to Eric. Exact outbound RTP
captures were consecutive and correctly paced (zero sequence/timestamp gaps), so
transport is not the current lead.

The deployed Voicebook image remains unchanged and tagged for rollback:

- accepted image: `sha256:3b28aa8102d69b3214687a7e732dcdeca35b8a11ab0d34187e1dad3f9b4472f7`
- host tag: `voicebook-stream:accepted-20260725`
- runtime: `torch==2.11.0+cu128`, `torchaudio==2.11.0+cu128`, CUDA 12.8

Two isolated images were built without replacing the accepted service:

- `voicebook-stream:torch211-cu130`
  (`sha256:8d59b93d1948d6839fe7bd20ec68b799fc48b765ab37bb1d14cc2c6a330bfcbc`):
  matched torch/torchaudio 2.11 CUDA 13.0, native `sm_120`, real BF16 CUDA
  matrix operation passed, health/warmup passed, and a 141-character response
  produced 9.04 seconds of PCM in 3.78 seconds.
- `voicebook-stream:accumulated-cu128`
  (`sha256:d621b1384a0a31f06c24b1d3f93909d41ec30379fd6fee65a5b4f92b5c9ced82`):
  exact accepted image plus one change in
  `generate_voice_clone_streaming()`—disable the transition from accumulated
  decoding to the 25-frame sliding decoder. It remained faster than real time:
  8.88 seconds of PCM in 6.38 seconds and 16.0 seconds in 11.85 seconds.

Why the decoder candidate exists: after roughly three seconds at the deployed
12-step chunk size, Faster-Qwen switches from exact accumulated decoding to a
sliding window. It trims context using a samples-per-frame ratio calibrated once
on early audio. That is a plausible source of recurring stitch errors in long
utterances, but it is not yet an accepted cause.

An offline waveform A/B was attempted with greedy generation and fixed Python,
NumPy, Torch, CUDA, and hash seeds. The repeatability gate failed: identical
requests in one resident process still produced different byte lengths and
hashes. Therefore no cross-file audio comparison is admissible as causal proof.

Methodology rule: freshly generated TTS samples are not controlled A/B inputs
merely because their text, seed, process, and settings match. First prove byte
repeatability. If that gate fails, capture one source generation and apply both
downstream treatments to those exact bytes. Otherwise generation variance is an
uncontrolled variable and the comparison is a null.

Morning gate: deploy only `accumulated-cu128` on the same GPU/service endpoint,
preserve the accepted container/image as rollback, and make one natural
45–60-second phone call with a long expressive response. Eric's ears decide the
warble gate. If unchanged, roll back and test the CUDA 13 image separately. Do
not combine both candidates in one call.

SoX is installed in the CUDA 13 bench image for completeness, but it is not an
artifact candidate for the resident 12 Hz model. The reachable `sox_norm` code
belongs to tokenizer V1/25 Hz; the loaded 12 Hz tokenizer V2 encoder contains no
SoX path, and the voice prompt is computed once at warmup and cached. The sample
rate inference warning also falls back to the correct 24000 Hz and the service
rejects any backend chunk whose actual rate differs.
