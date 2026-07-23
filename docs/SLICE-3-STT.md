# Slice 3 — Sumi STT: Parakeet/Riva streaming via the official plugin — LANDED ✅

Capability PASS (Yua second-read, 2026-07-23): service-DNS reachability, official-plugin
streaming recognition, and a managed Parakeet are all proven. New behavior: Sumi's ears work.

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

### Intentional live/canonical drift — `start_period`

The migrated **live** container carries `start_period=180s` (it was NOT recreated just to change a
healthcheck-timing field — that would repeat the 45-min build). The **canonical committed**
definition is `start_period=900s`, conservatively above the observed cold build. This drift is
deliberate: 900s applies at the next genuine recreate. (A 180s window marking a legitimately
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
