# Sumi voice-integration map (FROZEN for second read — recon 2026-07-23)

Read-only topology recon. **CONFIGURED** = exists in code/config; **RUNNING** =
verified live now. No implementation mutation performed. The accepted F1 TTS
container stayed healthy + guarded throughout recon.

## 1. Which becomes Sumi — MODIFY the `party` agent (a graduation, not new/rename)

- `agents/party` is explicitly the Sumi precursor. Its own `src/agent.py`:
  *"Nyla-on-the-chained-pipeline until it graduates into Sumi"*; persona is
  Nyla-on-that-pipeline "until Sumi."
- Party is the **chained STT→LLM→TTS** baseline (vs the realtime Gemini
  native-audio agents aoi/nyla/yua). CONFIGURED party components are all CLOUD:
  STT OpenAI Whisper-1, VAD Silero, LLM Gemini `gemini-3.1-flash-lite-preview`,
  TTS ElevenLabs `eleven_flash_v2_5`.
- **Sumi = graduate party to the LOCAL stack:** STT→Parakeet, LLM→Momo Qwen,
  TTS→voicebook-stream (`sumi-v1` voice), Silero VAD stays, persona→Sumi.
- DECISION: modify `agents/party` in place vs fork `agents/sumi` from it.

## 2. LiveKit / SIP dispatch path (CONFIGURED; NOT RUNNING)

- Path: Twilio DID → `livekit-sip` inbound trunk → `sip-dispatch-<agent>.json`
  rule (matches dialed DID) → LiveKit room `phone-<agent>` → the `phone-<agent>`
  worker (voice-agent container) joins and talks.
- Images: `livekit/livekit-server:v1.13.3` (ws://livekit-server:7880 internal),
  `livekit/sip:v1.6.0`, `livekit/egress:v1.13.0`. Dispatch rules exist for
  aoi/nyla/party/yua; **none for sumi**.
- RUNNING check: livekit-server, livekit-sip, and ALL voice-agent-* containers
  are currently **stopped/absent** (not in `docker ps`). This whole plane needs
  bring-up for a call.
- FOR SUMI: `sip-dispatch-sumi.json` (a DID → room `phone-sumi`) + the agent
  registers as `phone-sumi`. DECISION: new DID vs reuse.

## 3. Parakeet ASR (RUNNING; adapter NOT wired)

- `parakeet-ctl` (nvcr parakeet-1-1b-ctc-en-us NIM) RUNNING; **Riva gRPC
  `127.0.0.1:50051`** + HTTP health `9000` (ready=200). Streaming CTC.
- Adapter status: NO agent currently uses it (party uses the OpenAI Whisper
  plugin). Needs a LiveKit Riva/NIM STT plugin (or custom adapter) targeting
  `50051`. DECISION: which plugin (e.g. livekit-plugins-nvidia riva) + streaming
  config.

## 4. Momo LLM (RUNNING)

- `llama-server` (llama.cpp SYCL, 2 Intel GPUs SYCL0/1, `-ts 1.0/1.0`) serving
  **Qwen3.6-35B-A3B (uncensored-heretic Q4_K_M)** (+ BF16 mmproj), **port 8083**
  (`0.0.0.0`, `/health`=200), auth **api-key-file** `/etc/momo-llm/llama-api-keys`,
  **`-np 2` slots**, **384K ctx** (`-c 393216`), `-fa 1 -cb`, aliases
  `qwen3.6-35b-a3b, qwen36-35b-a3b, gemma-4-26b-a4b`.
- LiteLLM route EXISTS (DB-managed, params encrypted at rest): model IDs
  **`qwen/qwen3.6-35b-a3b`** and `qwen/qwen3.6-35b-a3b-fast`. (LiteLLM also
  carries gemini-3.x, xai/grok-4.5, deepseek, minimax, openrouter, tama gemma —
  unrelated to Sumi.)
- DECISIONS: (a) route **direct** momo:8083 (lower latency, api-key) vs **via
  LiteLLM** mizuki:4000 (spend-logging/cache/retry, one hop); (b) canonical
  `qwen/qwen3.6-35b-a3b` vs `-fast` for the call; (c) confirm the loaded
  "uncensored-heretic" quant is the intended Sumi brain.

## 5. voicebook-stream endpoint + stable service definition

- Endpoint (RUNNING, accepted F1): `127.0.0.1:5060` — `POST /speak/stream`
  (raw s16le PCM, 24000 Hz mono, X-Audio-Format/X-Sample-Rate/X-Channels),
  `POST /speak` (audio/wav), `GET /healthz`. F1 image
  `sha256:3b28aa8102d6…`, currently the **temp `voicebook-stream-qual`**
  container (docker-run, not managed).
- **No stable service definition exists** (only `docker-compose.tts.yaml` for
  the old tts). NEEDED: a managed def (e.g. `docker-compose.stream.yaml` modeled
  on tts) pinning the F1 **digest**, RO hf-cache + `/srv/voicebook` + registry,
  restart policy — replacing the temp qual container. DECISION: keep port 5060
  vs take 5055 after tts retires; service name.

## 6. Cloud dependencies / fallbacks

- Sumi's TARGET path is **fully local** (Parakeet + Momo + voicebook-stream) —
  REMOVE from her active path: OpenAI Whisper, Google Gemini, ElevenLabs.
- KEEP as explicit ROLLBACK (documented, not active): party's cloud chain
  (Whisper/Gemini/ElevenLabs); optionally a Gemini-native-audio Sumi. LiteLLM's
  cloud models stay for the OTHER agents (aoi/nyla/yua) — not removed.

## 7. Final resident loadout / VRAM (no ghosts)

- **mizuki** (RTX 5060 Ti, 16311 MiB): parakeet tritonserver 3630 +
  voicebook-stream-f1 python 6214 = 9844 used, **5981 free**. voicebook-tts
  STOPPED (rollback). The `voice-agent-sumi` container is CPU orchestration (no
  GPU). No ghosts (exactly the two expected GPU procs).
- **momo** (2× Intel GPU): Qwen model resident + serving; `-np 2`, split across
  both GPUs. **GAP:** clean per-GPU VRAM readback unavailable (`xpu-smi` returned
  N/A). DECISION/action: get a working Intel VRAM/headroom readback before
  committing Sumi's LLM slot.

## 8. End-to-end test spec (synthetic first, then Eric human-mic)

Latency marks on EVERY turn: **t0 speech-start**, **t1 ASR-final**
(Parakeet transcript), **t2 LLM-first-token** (Momo), **t3 TTS-first-audio**
(voicebook-stream TTFA), **t4 playback-start**.
- **Synthetic turn:** feed a fixed caller utterance (audio file or injected
  transcript) → assert correct transcript, a coherent Momo reply, Sumi audio out
  in her voice; record t1–t4 and end-to-end first-audio latency.
- **Eric human-mic call:** Eric dials the Sumi DID → real conversation → same
  t1–t4 marks + Eric's subjective identity/quality (does she sound like Sumi,
  any clicks/warble/seams, is the latency conversational).
- Target: first-audio well under ~1.5 s; voicebook TTFA ~230 ms already measured.

## 9. Rollback at every component boundary

| Boundary | Primary (Sumi) | Rollback |
|---|---|---|
| Agent | Sumi local chain | party cloud chain / Gemini-native Sumi |
| STT | Parakeet (Riva 50051) | OpenAI Whisper-1 |
| LLM | Momo Qwen (direct or LiteLLM) | Gemini text via LiteLLM |
| TTS | voicebook-stream F1 (5060) | voicebook-tts unary (restart) / cu128-clean image / ElevenLabs |
| SIP/LiveKit | dispatch-sumi → phone-sumi | rule disabled → call fails clean / routes to party |

Each swap is agent-config level → rollback = revert config + restart the agent.

## Open decisions (consolidated)

1. Modify `party` in place vs fork `agents/sumi`.
2. Momo routing: direct 8083 vs LiteLLM 4000 (latency vs observability).
3. Parakeet STT plugin choice + streaming config.
4. Confirm the loaded uncensored-heretic quant + `-a3b` vs `-a3b-fast`.
5. voicebook-stream stable service def: port + name + digest pin.
6. Retire voicebook-tts (5055) now vs keep as rollback (Yua: keep as rollback).
7. Sumi SIP DID: new vs reuse.
8. Momo Intel-GPU VRAM headroom readback method (current gap).
9. Bring up the stopped LiveKit plane (server + sip + agent) for the call.
