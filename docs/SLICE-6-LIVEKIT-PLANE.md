# Slice 6 — LiveKit plane bring-up + isolated Sumi worker — LANDED ✅

> **Current production route (2026-07-26):** Sumi calls
> `http://sumi-local-llm:8080/v1` directly with model `qwen3.5-9b`, per-request
> thinking disabled, and the explicit non-secret OpenAI-client placeholder
> `SUMI_LLM_API_KEY=local-no-auth`. There is no LiteLLM or 1Password hop in this
> path. Older LiteLLM key material below is historical rollback/design context,
> not a launch prerequisite.

New capability: the media plane is live and an **isolated** Sumi worker is
registered against it as `phone-sumi`, waiting for dispatch. No SIP, no DID, no
inbound routing, no Party retirement — the guardrail line ("no SIP/DID mutation
or live-call routing until the isolated synthetic turn passes") is intact.

## What came up (mizuki, 2026-07-23)

Pre-state: the media plane was **entirely down** (nothing on 7880/7881/7882/5060/
6379) and **Party was not running** — a clean slate, no live service to disrupt.

- **redis + livekit-server only**, from `docker-compose.yaml`:
  `docker compose -f docker-compose.yaml up -d redis livekit-server`
  (livekit-sip and livekit-egress deliberately NOT started). Both healthy;
  `GET :7880/` → HTTP 200. livekit-server is on `voice_default`, the same network
  as parakeet-ctl and voicebook-stream, so the worker reaches all three by DNS.

## The isolated Sumi worker

- **Image:** `voice-agent:sumi` — built from `Dockerfile.agent` at `b8e6ce9`. The
  shared `voice-agent:latest` was left untouched (isolation).
  - **Fixed a latent infra break along the way:** the agent image had not built
    since `services/*` joined the uv workspace — `uv sync --frozen` failed with
    "Distribution not found at .../services/voicebook-stream". `Dockerfile.agent`
    now copies `services/` (light deps, no GPU/torch enters the image; the agent
    runtime never imports them). Committed `b8e6ce9`. This unbreaks *every*
    agent's image build, not just Sumi's.
- **Run (single container, not the agents compose):**
  ```
  # secrets/livekit-agents.env supplies the shared LiveKit/Musubi runtime values.
  # local-no-auth is an explicit non-secret placeholder required by the
  # OpenAI-compatible client; it is not a bearer and does not route through LiteLLM.
  docker run -d --name voice-agent-sumi --restart unless-stopped --network voice_default \
    --env-file secrets/livekit-agents.env \
    -e AGENT=sumi -e LIVEKIT_URL=ws://livekit-server:7880 \
    -e LIVEKIT_VOICE_LOGS=/app/logs/voice \
    -e SUMI_LLM_MODEL=qwen3.5-9b \
    -e SUMI_LLM_BASE_URL=http://sumi-local-llm:8080/v1 \
    -e SUMI_LLM_API_KEY=local-no-auth \
    -e SUMI_LLM_DISABLE_THINKING=true \
    -v "$PWD/logs/voice:/app/logs/voice" voice-agent:sumi-<shortsha>
  ```
- **Recovery:** the worker uses `restart=unless-stopped`, matching the managed
  local services. Docker brings it back after daemon/host recovery; a crash is
  restarted rather than leaving the phone route silently without a worker.
- **No LLM secret in the current route.** The worker reaches only the local
  `sumi-local-llm` service on `voice_default`; `local-no-auth` satisfies the
  client library's non-empty API-key parameter and grants nothing anywhere.
- **Fail-loud gates all passed** (the container did NOT crash-loop, restarts=0):
  persona present, `MUSUBI_V2_TOKEN_SUMI` present, direct base URL explicit,
  `SUMI_LLM_API_KEY=local-no-auth`, and Qwen thinking disabled.

## Proof

```
registered worker  agent_name=phone-sumi  id=AW_wWUMsUFaodwY  url=ws://livekit-server:7880
plugins: livekit.plugins.nvidia (STT), livekit.plugins.openai (LLM), livekit.plugins.silero (VAD)
status=running  restarts=0  otelServiceName=voice-sumi
```

The worker is explicit-dispatch only (`@server.rtc_session(agent_name="phone-sumi")`):
it does nothing until a job is dispatched to it — there is no inbound phone path.

## Rollback (one command each, fully reversible)

- Worker: `docker rm -f voice-agent-sumi`
- Plane: `docker compose -f docker-compose.yaml down` (redis state persists in the
  `voice_redis_data` volume; `down -v` would wipe it — don't, it holds SIP routing
  for the real deploy).
- The legacy `sumi-voice-worker-v2` LiteLLM alias is not used by the current
  worker. Do not provision, revoke, or delete it based on this page; any future
  LiteLLM migration requires its own live-state readback and authorization.

## Current acceptance / next

The 2026-07-26 synthetic turn passed on the direct stack: Parakeet → local
Qwen3.5-9B → Voicebook TTS, with a captured response and zero service restarts.
The remaining acceptance is Eric's real PSTN call through Twilio and
`livekit-sip`; the synthetic client does not traverse that media path.
