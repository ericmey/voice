# Slice 3 — Sumi STT: faster-whisper accepted for the phone path — LANDED ✅

The original Parakeet/Riva capability passed on 2026-07-23.  The Monday-demo
qualification on 2026-07-24 deliberately reopened the provider choice after
Parakeet proved operationally expensive and brittle across host recovery.
Sumi's accepted default is now local `faster-distil-whisper-large-v3` through
Speaches and LiveKit's maintained `STT` + `StreamAdapter` path.

This is the production-shaped choice, not the novelty choice:

- Silero owns endpointing; after end-of-speech, LiveKit sends the utterance to
  Speaches' OpenAI-compatible batch transcription endpoint.
- Speaches and the exact model are local on Mizuki, isolated to physical GPU 2
  (`device_ids: ["1"]`). No cloud STT or automatic provider fallback exists.
- Service startup loads the model **and runs a real warm transcription** before
  Docker reports healthy. A container restart was inference-ready in 6 seconds;
  the first caller is never the CUDA warmup request.
- The model stays resident (`WHISPER__TTL=-1`); its measured idle allocation is
  about 2.2 GiB on the 16 GiB card.

## Qualification decision

All candidates received the same 16 kHz mono inputs. Latency below is paced
wall-clock audio, measured from the last input frame to the final transcript.

| Gate | faster-whisper (accepted) | sherpa/Nemotron 0.6B 560 ms |
|---|---|---|
| General phrase | exact | dropped “what is” |
| Numbers: “35% of 40 equals 14” | exact content | lost the numbers; “Forty equals fourteen” |
| Domain-term synthetic stress | WER 0.3333 | WER 0.5556 |
| Warm final after audio | 0.240–0.312 s | fast, with useful interim results |
| Runtime cost | ~2.2 GiB GPU 2 | ~1.16 GiB host RAM, CPU-only |
| Monday gate | **PASS** | **FAIL accuracy** |

The domain fixture is intentionally a stress test, not a claim that synthetic
macOS speech represents Eric's microphone. Both candidates need more real-call
domain audio before claiming proper-noun perfection; faster-whisper still won
the identical-input comparison and the general/numeric gates decisively.

The first controlled phone discriminator then narrowed the remaining error on
real PSTN audio. Faster-whisper preserved `Mizuki`, `35%`, and `1030 Monday`,
but transcribed utterance-initial `Sumi` as `Subi`; the 7.568-second utterance
completed in 444 ms with no service restart or provider error. The accepted
targeted repair passes an environment-overridable expected-vocabulary prompt
through the existing OpenAI transcription request. Its provider-handoff test
was witnessed red with that argument removed, then green after restoration.

The identical controlled retest (`SCL_XfCuHn245oWS`) closed the lane. The
literal transcript was `Sumi, schedule the Mizuki review for 35% at 1030
Monday, then repeat it back.` All four acceptance gates passed: utterance-
initial `Sumi`, `Mizuki`, `35%`, and the meaning of `10:30 Monday`. Speaches
transcribed the 7.728-second phone utterance in 455 ms over HTTP 200; the
worker, Speaches, LiveKit, SIP, Redis, and voicebook all remained at restart
count zero. The before/after was one-variable evidence: `Subi` before the
domain prompt, `Sumi` after it, with no measured latency regression.

The pinned Speaches release also exposes an older OpenAI realtime websocket.
That path was rejected for production: it buffers until VAD completes and then
calls the same batch endpoint, while adding version-specific session-schema and
loopback defects. `StreamAdapter` gives the same utterance-final behavior through
maintained LiveKit surfaces without a compatibility fork.

## Accepted topology

- **Sumi worker:** `SUMI_STT_PROVIDER=faster-whisper` (also the fail-loud code
  default) → `http://speaches-stt:8000/v1` by service DNS.
- **Endpointing:** the same local Silero VAD used by `AgentSession`.
- **Transcription:** LiveKit OpenAI `STT(use_realtime=False)` wrapped in
  `livekit.agents.stt.StreamAdapter`; `SUMI_WHISPER_STT_PROMPT` carries the
  expected House vocabulary and remains operator-overridable.
- **Service authority:** `deploy/speaches-stt/docker-compose.speaches-stt.yaml`;
  immutable image digest, GPU 2 pin, persistent Hugging Face cache, warm-on-every-
  start entrypoint, model-aware healthcheck, `restart: unless-stopped`.
- **Operations:** `make speaches-stt-up|down|logs`.
- **Rollback/evaluation:** `SUMI_STT_PROVIDER=sherpa` for CPU Nemotron;
  `SUMI_STT_PROVIDER=parakeet` for Riva. Unknown providers abort startup.

## Historical Parakeet/Riva qualification (rollback only)

## Topology

- **STT plugin:** `livekit-plugins-nvidia==1.6.5` (matches `livekit-agents~=1.6.5` — **no agents
  bump**). It uses `riva.client` with a `server` override + `use_ssl=False`, so it drives our
  self-hosted **insecure** Riva directly (not only NVIDIA cloud).
- **Sumi worker config** (`agents/sumi/src/agent.py`): `nvidia.STT(server="parakeet-ctl:50051",
  use_ssl=False, api_key="", model="parakeet-1.1b-en-US-asr-streaming", sample_rate=16000,
  language_code="en-US", punctuate=True)`. Server + model are env-overridable
  (`SUMI_STT_SERVER` / `SUMI_STT_MODEL`).
- **Gotcha fixed:** the plugin's default model (`…-silero-vad-sortformer`) is NOT served here.
  parakeet-ctl advertises exactly ONE ASR model, `parakeet-1.1b-en-US-asr-streaming`
  (streaming/online/16k/en-US), via `GetRivaSpeechRecognitionConfig`. The default would fail
  "model unavailable"; the served name is pinned.

## Parakeet is now MANAGED (it wasn't)

parakeet-ctl was an **unmanaged bare `docker run`** — `restart=no`, no compose/systemd/ctl — that
did not survive reboot and was itself a launch blocker. Its authoritative definition now lives in
`deploy/parakeet/docker-compose.parakeet.yaml`:

- pinned to the immutable digest `sha256:5f30bb5fbb6e…`;
- attached to `voice_default` so the worker reaches it by service DNS `parakeet-ctl:50051`;
- loopback publishes `127.0.0.1:50051` / `:9000` PRESERVED (host riva clients / ops);
- `restart: unless-stopped`; both-surface healthcheck (ready AND live);
- reproduces ONLY the real run-overrides diffed against the image (`NIM_MODEL_PROFILE`, the
  `/opt/nim/.cache` bind, `--gpus all`, shm, ports) — the other ~93 env vars are image defaults,
  intentionally not copied.

**Migration** (2026-07-23): baseline ready/live 200 → `docker stop` + **rename**
`parakeet-ctl` → `parakeet-ctl-prev` (preserve, not remove) → compose up managed → the NIM ran its
~45-min one-time engine build (`riva-deploy`: `.rmir` → Triton repo + FP8/TensorRT) → ready/live
200 + voice_default REACH. `parakeet-ctl-prev` remains **stopped as the rollback tier**.

**Current disposition (2026-07-24):** the post-reboot GPU-pinned recreate was
stopped and removed while still rebuilding. Its incomplete writable layer had
grown to 19.3 GiB; removing it reclaimed about 18 GiB. The prior stopped
`parakeet-ctl-prev` rollback container was not touched. Parakeet is not running
and is no longer on Sumi's Monday path.

### Historical live/canonical drift — `start_period`

The migrated container carried `start_period=180s` (it was NOT recreated just to change a
healthcheck-timing field — that would repeat the 45-min build). The **canonical committed**
definition is `start_period=900s`, conservatively above the observed cold build. This drift is
closed now that the managed recreate is absent: 900s applies at the next genuine recreate. (A 180s window marking a legitimately
*building* container "unhealthy" is the same misclassify-an-expected-state defect fixed in
monitoring the same day.)

## Proofs

Both drove the official plugin exactly as the worker configures it:

- **shared-netns** (`--network container:parakeet-ctl`, `127.0.0.1:50051`): `"Hi Eric, it is Sumi.
  I can speak and this is my voice."` — word-for-word.
- **voice_default DNS** (`--network voice_default`, `parakeet-ctl:50051`) — the integration gate:
  `"Hi Eric, it is Sumi. I can speak and this my voice."` — a **one-word deletion** ("is"). This is
  an honest ASR-accuracy datum for the later real-call review, NOT an integration blocker; the
  capability (official plugin transcribes via service DNS through managed Parakeet) is proven.

## Not yet up (expected, not broken)

The LiveKit plane is OFF — no livekit-server / livekit-sip / redis / voice-agent; nothing on
7880/7881/7882/5060. It simply hasn't been brought up in this build. Remaining path: Slice 4 Momo
LLM → Slice 5 voicebook-stream TTS adapter → isolated Sumi worker → LiveKit/SIP → synthetic turn →
the real call.
