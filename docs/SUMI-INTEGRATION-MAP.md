# Sumi voice-integration map — REVISED (second-read corrections folded, 2026-07-23)

Read-only recon + design. **CONFIGURED** = in code/config; **RUNNING** = verified
live; **GAP** = unresolved/unreadable; **DECISION** = resolved below. No
implementation performed. F1 TTS container stays healthy + guarded.

## Resolved decisions (Yua second-read 3/3)

- **Sumi = fork `agents/sumi` FROM party** (party stays UNCHANGED as rollback).
  Retire party + restore the 4-agent steady state separately, after the real
  call passes.
- **Reuse party's current DID** for Sumi; preserve the old dispatch-rule body for
  rollback. No new number.
- Keep old `voicebook-tts` **stopped on 5055** (rollback).
- Current Momo Qwen quant = **candidate brain**; Sumi **identity + concurrency**
  are acceptance gates.
- Intel per-GPU VRAM readback = useful, **non-blocking** (Qwen resident);
  replaced by real **two-slot contention / latency / cache-reuse / eviction**
  tests (+ host-memory watcher, see item 4 / memory triage below).
- Bring up LiveKit **only after** the 5060 conflict is removed and a clean
  preflight passes.

## 1. Sumi identity (item 5 of 2nd-read) — FROZEN, fail-loud, isolated

Fork `agents/sumi` from `agents/party`. FREEZE and assert-at-startup:
- persona source = `agents/sumi/prompts/system.md` (Sumi, not Nyla-on-party);
- `AgentConfig` name = `sumi`; worker registration `agentName = phone-sumi`;
- Musubi **namespace/tag = Sumi's own** (NEVER party/voice); token +
  `agent-entrypoint.sh` `$AGENT=sumi` → `MUSUBI_V2_TOKEN_SUMI`; telemetry name
  `sumi`.
- **Fail-loud startup:** refuse to start if persona/token/namespace/model/voice
  are missing or resolve to party's. **Sumi must never write to party/voice
  memory.**

## 2. LiveKit / SIP dispatch (CONFIGURED; plane STOPPED)

- Path: Twilio DID → `livekit-sip` (**`network_mode: host`**, SIP **5060**) →
  dispatch rule (matches dialed DID) → **generated** room (roomPrefix `phone` +
  unique suffix — `dispatchRuleIndividual` does NOT make a fixed `phone-sumi`
  room) → the **`phone-sumi` worker** is dispatched into that generated room.
  Room identity ≠ worker registration; keep them separate.
- livekit-server v1.13.3 (ws://livekit-server:7880), sip v1.6.0. All voice-agent
  and livekit containers currently **stopped**.
- Sumi: add `sip-dispatch-sumi.json` reusing party's DID (preserve party's rule
  body for rollback); Sumi registers `agentName=phone-sumi`.

## 3. voicebook-stream TTS — service + adapter (item 9 + 4)

- **RUNNING**: F1 image `sha256:3b28aa8102d6…` as the temp `voicebook-stream-qual`
  (docker-run). Endpoint `/speak/stream` (raw s16le PCM 24k mono), `/speak`
  (wav), `/healthz`.
- **STABLE SERVICE DEF (build first slice):** managed compose — pin image by
  **digest 3b28aa8102d6**, `pull_policy: never`, RO mounts (hf-cache,
  `/srv/voicebook`, registry), **healthcheck**, `restart: unless-stopped`, bind
  **`127.0.0.1:5056`** (NOT 5060 — livekit-sip owns host 5060), startup **image
  readback** asserting the digest. The external qual watcher is NOT the final
  service manager.
- **TTS ADAPTER (custom — the real gap):** a `livekit.agents.tts.TTS` subclass
  wrapping `POST /speak/stream`, with **cancellation** (close the HTTP stream on
  interrupt) and **backpressure**. No official plugin covers this raw endpoint.

## 4. Parakeet STT — adapter (item 4)

- **RUNNING**: parakeet-1-1b-ctc NIM, **Riva gRPC `127.0.0.1:50051`** + health
  `9000` (200). Streaming CTC.
- **Adapter:** the **official `livekit-plugins-nvidia`** Riva STT plugin EXISTS
  (verified via LiveKit docs) — use it, do NOT custom-build STT. At the STT
  slice: pin a version compatible with `livekit-agents~=1.6.5`, point it at
  parakeet-ctl Riva gRPC `50051` (self-hosted, likely no TLS on loopback), and
  prove streaming partial/final + EOU against the live server.

## 5. Momo LLM route (item 6) — NEW readable contract

- **RUNNING**: llama.cpp SYCL (2 Intel GPU, `-ts 1.0/1.0`),
  Qwen3.6-35B-A3B-uncensored-heretic **Q4_K_M** (+mmproj), **momo:8083**
  (`/health` 200 in ~0.5ms), auth **api-key-file** (LiteLLM holds it as
  `MOMO_API_KEY`), **`-np 2` slots**, `-c 393216` = **~196608 (192K) per slot**
  (aggregate ÷ slots, NOT per-conversation), `-fa 1 -cb`.
- LiteLLM route exists (`qwen/qwen3.6-35b-a3b`, `-fast`) but its params are
  **SALT-encrypted / unreadable** via admin `/v1/model/info` (GAP).
- **DECISION:** create a **NEW readable Sumi LiteLLM route** — defined
  timeout/retry, explicit `cache_prompt`, **ZERO automatic cloud fallback** — as
  primary (observability). **Do NOT mutate the opaque existing route.** Direct
  `momo:8083` (with `MOMO_API_KEY`) is the control comparison. Verify what
  `-fast` actually is before using it (don't choose by name).
- **Two-slot concurrency test (acceptance gate):** real 2-caller contention,
  per-slot latency, KV **cache reuse**, and **eviction** proof — under a
  **host-memory watcher** (below).

## Momo host-memory constraint (CID alert-cbed611428bbdd26 — ACCEPTED: TRUE capacity, NOT incident)

Stable-but-tight, verified read-only 2026-07-23:
- MemAvailable 2.3 GiB / **7.4%** of 30Gi; PSI some/full avg10/60/300 = **0.00**.
- cgroup `momo-llama-qwen36-35b.service`: MemoryMax/High = **infinity** (no
  limit); memory.current ~24.8GB; memory.events **high=0 max=0 oom=0
  oom_kill=0** (zero events).
- swap: `/dev/zram0` **2.7G used** (prio 100, compressed RAM-backed); disk
  `/swap.img` 64G **0B used** (prio -2). No disk-swap churn (vmstat si/so ~0).
- Cause = llama-server 24.8GB RSS = model residency (Q4_K_M 20G mmap + KV);
  `/health` 200 in ~0.5ms, no degradation.

**DO NOT assert calls will grow KV / exhaust RAM — UNPROVEN.** llama.cpp may
**pre-reserve** much of the configured KV at startup (the 24.8GB may already
include it). **Measure** allocation behavior from process RSS + cgroup
`memory.current` deltas across controlled requests (idle → 1 call → 2 calls)
before any capacity claim.

**Scope (Yua):** this does NOT block building/proving ONE Sumi call; it gates
any **two-caller production-capacity** claim. A later distress rule should
combine low-available WITH PSI / swap-churn / OOM / latency (recorded for
Shiori).

## 6. Cloud deps / fallbacks

Sumi's active path is **fully local** (Parakeet + Momo + voicebook-stream).
Remove from her path: OpenAI Whisper, Google Gemini, ElevenLabs. Keep as
**documented rollback** (not active): party's cloud chain; optional
Gemini-native Sumi. LiteLLM cloud models stay for the OTHER agents.

## 7. Resident loadout / VRAM (no ghosts)

- **mizuki** (RTX 5060 Ti 16311 MiB): parakeet 3630 + voicebook-stream-f1 6214 =
  9844 used / **5981 free**; tts stopped (rollback); `voice-agent-sumi` is CPU
  orchestration. No ghosts.
- **momo** (2× Intel GPU): Qwen resident, `-np 2`, split. Per-GPU VRAM readback
  a GAP (xpu-smi N/A) — non-blocking; concurrency proven by the two-slot test.

## 8. E2E test spec — latency marks (item 7)

Per turn record: **speech_end**, ASR **partial / final / EOU**, LLM **first
token**, **first text chunk sent to TTS**, **first TTS byte**, **playback
start**. **PRIMARY latency = speech_end → playback_start.** Also test
**open-stream silence endpointing** and **repeated warm turns** (cache reuse).

FINAL PROVE-SEQUENCE (Yua) — one call first, capacity later:
1. **Single-client synthetic integration** — injected utterance → correct
   transcript, coherent Momo reply, Sumi audio in her voice; capture all marks.
2. **Eric real human-mic call** (reused DID) — same marks + Eric's identity /
   quality judgment. The memory alert does NOT block this.
3. **Instrumented two-client contention** (gates 2-caller capacity only): STAGED
   — idle baseline → one realistic call → two concurrent realistic calls →
   controlled context ramp ONLY if safe. Under a **momo host-memory watcher**
   sampling MemAvailable, PSI, swap-in/out, OOM counters, Qwen health, latency,
   and process/cgroup `memory.current` deltas. On any boundary: **stop CLIENTS
   first**; never kill/restart Qwen without a separate decision.
4. **Optional max-context stress** — only after (3), explicitly.
DO NOT begin with two filled 192K slots — that is a stress test, not the demo.

## 9. Rollback — scoped actions + pre-mutation readbacks (item 8)

Not all agent-config. Record the exact action + a pre-mutation readback for each:

| Boundary | Primary | Rollback action (scoped) | Pre-mutation readback |
|---|---|---|---|
| Agent | agents/sumi | stop sumi worker, start party worker | current running worker list |
| STT | nvidia Riva plugin | agent config → Whisper; restart worker | agent stt config |
| LLM | new Sumi LiteLLM route | agent → old route / direct momo | route id + params |
| TTS | voicebook-stream 5056 (F1) | restart voicebook-tts 5055 (unchanged) | container id/digest + health |
| SIP | dispatch-sumi (in **Redis**) | re-apply party's preserved rule body | dump current SIP rules from Redis |
| LLM route (LiteLLM) | new route (DB) | delete new route row (never touch opaque one) | DB row snapshot before insert |

## Implementation slices (locked order; NO implementation until map green)

1. **Stable TTS service** (compose, digest-pinned, 5056, healthcheck) →
2. **Sumi identity/package** (fork, freeze, fail-loud, memory isolation) →
3. **STT adapter** (nvidia Riva plugin vs parakeet 50051) →
4. **LLM adapter/route** (new readable Sumi route, zero cloud fallback) →
5. **TTS adapter** (custom livekit TTS, cancellation/backpressure) →
6. **SIP/LiveKit bring-up** (after 5060 clear + clean preflight) →
7. **Single-client synthetic E2E** (latency marks) →
8. **Eric human-mic call** — the demo; proves ONE Sumi call.

Then, gating 2-caller **capacity** only (NOT the demo):
9. **Instrumented two-client contention** (staged, under the momo host-memory
   watcher; stop clients first on boundary) →
10. **Optional max-context stress** (later, if useful).

## Open GAPs to resolve during slices

- Exact `livekit-plugins-nvidia` version vs 1.6.5 + Riva-gRPC config (STT slice).
- `-fast` alias semantics (LLM slice).
- Momo Intel per-GPU VRAM readback method (non-blocking).
- Opaque existing LiteLLM qwen route params (do not read by mutation; build new).
