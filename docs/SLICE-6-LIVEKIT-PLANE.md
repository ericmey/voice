# Slice 6 — LiveKit plane bring-up + isolated Sumi worker — LANDED ✅

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
  # SUMI_LLM_API_KEY is resolved from 1Password by `op run` (config/sumi-llm-key.env.tpl, D1)
  # and passed by NAME (-e SUMI_LLM_API_KEY, no value) — kept out of repo/tmp/argv/history.
  # (It does land in the container's env metadata, inherent to running the worker; never
  # print or `docker inspect` that env.)
  # secrets/livekit-agents.env still supplies the other runtime values. HAZARD: op run
  # injects at CREATE time — always launch via op run; a bare `docker run` drops the
  # injection (the op-run-stack deploy hazard). A later `docker start` of the SAME
  # container reuses the env baked at create, so restarts/rollback are unaffected.
  # LEAST PRIVILEGE (D1): op-run injects ONLY the Sumi key (config/sumi-llm-key.env.tpl),
  # not the 8-ref livekit.env.tpl — otherwise the docker CLI process would receive eight
  # unrelated resolved secrets (LiveKit key/secret, OpenAI, Google, ElevenLabs, three Musubi
  # tokens) it never needs. The container's other runtime values come from
  # secrets/livekit-agents.env; SUMI is passed by name into the container.
  op run --env-file=config/sumi-llm-key.env.tpl -- \
    docker run -d --name voice-agent-sumi --network voice_default \
    --env-file secrets/livekit-agents.env \
    -e AGENT=sumi -e LIVEKIT_URL=ws://livekit-server:7880 \
    -e LIVEKIT_VOICE_LOGS=/app/logs/voice \
    -e SUMI_LLM_API_KEY \
    -v "$PWD/logs/voice:/app/logs/voice" voice-agent:sumi-<shortsha>
  ```
- **Least-privilege LLM key.** Rather than the LiteLLM master key, the worker
  carries a **scoped virtual key** (`key_alias=sumi-voice-worker-v2`,
  `models=["sumi"]`) — it can call ONLY the `sumi` route, so even a bug can't
  reach another model or a cloud provider. Defense-in-depth on top of the route's
  own no-fallback.
- **Fail-loud gates all passed** (the container did NOT crash-loop, restarts=0):
  persona present, `MUSUBI_V2_TOKEN_SUMI` present, `SUMI_LLM_API_KEY` present.

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
- Scoped key — **dual-key lifecycle (do NOT conflate candidate rollback with prior-key
  retirement):** `sumi-voice-worker-v2` is the NEW candidate key that the accepted replacement
  worker *uses* — so it is revoked ONLY on the FAILURE branch, and KEPT on success.
  - **If the candidate deploy FAILS** and the OLD worker is restored + re-accepted: revoke/delete
    the v2 candidate artifacts — `POST /key/delete {key_aliases:["sumi-voice-worker-v2"]}` (source
    contract is `key_aliases`, not `keys`; verified) and delete the 1Password item — since nothing
    live depends on them.
  - **If the replacement SUCCEEDS:** KEEP `sumi-voice-worker-v2` active (the running worker needs
    it). Retire the PRIOR key / `/tmp` temp only AFTER the rollback window closes AND under a
    separate authorization — never as part of this deploy. **Verify the prior alias's metadata
    LIVE at execution** before retiring it (don't assume its name/scope from this doc).

## Next

Slice 7 — the **single-client synthetic turn**: dispatch `phone-sumi` to a room, a
synthetic caller publishes speech, and Sumi hears (Parakeet) → thinks (Momo) →
speaks (voicebook-stream) in one loop, captured with latency marks. That pass is
the guardrail threshold that unlocks Slice 8 (Eric's real call).
