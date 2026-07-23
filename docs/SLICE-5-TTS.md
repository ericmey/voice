# Slice 5 — Sumi TTS: her own voice via voicebook-stream (custom LiveKit plugin) — LANDED ✅

New behavior: Sumi speaks in **her own accepted master voice**, locally. The last
inherited cloud scaffold (ElevenLabs on Nyla's id) is gone — the pipeline is now
fully local end to end: Parakeet STT → Momo LLM → voicebook-stream TTS.

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

## Not yet up (expected, not broken)

The LiveKit plane is still OFF (nothing on 7880/7881/7882/5060). All three pipeline
components are now wired and individually proven. Remaining path: **isolated Sumi
worker → LiveKit/SIP bring-up (Slice 6) → single-client synthetic E2E (Slice 7) →
Eric's call (Slice 8)**. Slice 7 is where the three proven components run together
as one turn, in the worker, on the network where each endpoint is proven.
