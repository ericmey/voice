# Architecture

## Components

```
                 PSTN
                  │
                  ▼
           ┌──────────────┐
           │  Twilio SIP  │  Elastic SIP Trunk, Origination Connection Policy
           │   trunking   │  pointed at this box's public IP:5060/tcp
           └──────┬───────┘
                  │ SIP INVITE (TCP, digest-auth for outbound only)
                  ▼
       ┌──────────────────────┐
       │   livekit-sip        │  Docker container, host networking
       │   (container)        │  Parses INVITE → trunk + dispatch match
       └──────────┬───────────┘     → creates LiveKit room
                  │                 → dispatches the right agent
                  │ Redis coord + WebSocket to server
                  │
         ┌────────┴───────────┐
         ▼                    ▼
  ┌─────────────┐      ┌──────────────────┐
  │   redis     │      │ livekit-server   │  Docker container, bridge net
  │ (session    │      │ (WebRTC signaling│  Ports: 7880 (ws), 7881 (tcp
  │  state)     │      │  + TURN)         │         turn), 7882/udp (turn)
  └─────────────┘      └────────┬─────────┘
                                │ agent registers over WebSocket
                                ▼
                      ┌───────────────────────────────┐
                      │  Python voice agents          │  Docker containers
                      │  (nyla, aoi, yua, sumi)      │  voice-agent-<name>
                      │                               │
                      │  Each exposes @function_tool  │
                      │  methods for memory, household │
                      │  status, time, weather        │
                      └───────────────────────────────┘
```

## Workspace Packages

### `sdk/`
Shared Python package imported by the agents. Contains:

- **`config.py`** — `AgentConfig` dataclass. Exactly THREE fields: `agent_name`,
  `memory_agent_tag`, `musubi_v2_namespace`. Everything else derives from `agent_name` —
  `registration_name` (`phone-<name>`), the service name, the memory namespace. One root.
  (This page used to list `discord_room`, `musubi_v2_presence` and `household_presences`
  as fields. None of them exist. A config doc that names fields you cannot set is how
  someone spends an afternoon looking for a knob that was never there.)
- **`tools/`** (separate workspace member) — Core and Memory
  mixins that each agent inherits. Function-tool decorated methods expose
  capabilities to the voice model.
- **`telephony.py`** — `resolve_caller()` reads the SIP participant's
  attributes from a connected room. One hop behind the agent entrypoint.
- **`musubi_client.py`** — async HTTP client for the canonical Musubi API.
- **`trace.py`**, **`transcript.py`**, **`env.py`** — ancillary.

### `agents/nyla/`
Realtime voice persona. Gemini 2.5 Flash Native Audio, Aoede voice. Registers
as `phone-nyla`. Surveys the household via `household_status`.

### `agents/aoi/`
Realtime voice persona. Same model as Nyla, Kore voice, distinct prompt.
Technical partner; also surveys the household.

### `agents/yua/`
Realtime voice persona. Same model family as Nyla and Aoi, Leda voice,
Yua-specific prompt and memory namespace. Coding and QA partner, second
set of eyes, and Aoi's development partner; also surveys the household.

### `agents/sumi/`
Sumi Tachibana's fully-local chained STT/LLM/TTS line. Riva Parakeet ASR →
Silero VAD → Mistral Nemo (llama.cpp) → Orpheus TTS — every leg on mizuki's
Blackwell card, nothing leaving the box. Sumi's own persona (the household's keeper — bright, warm, chipper)
and her own memory channel (`sumi/voice`, distinct from `sumi/hermes`).

### `tools/`
Shared LiveKit `@function_tool` mixins composed by the agents. See
[tools/README.md](../tools/README.md).

## Call path

1. **PSTN dial** reaches Twilio on a DID.
2. Twilio's Origination Connection Policy routes the SIP INVITE to this
   host's public IP:5060 over TCP.
3. **livekit-sip** receives the INVITE. Looks up:
   - Inbound trunk match.
   - Caller allowlist check against `allowed_numbers` on the trunk —
     rejects at 486 if the FROM number isn't on the list.
   - Dispatch rule match by dialed DID (`numbers` field, not
     `inbound_numbers` — see DISPATCH-RULE-GOTCHAS.md).
4. livekit-sip creates a LiveKit room (`phone_<caller>_<random>`) and
   dispatches the right agent by agentName.
5. **Voice agent's `entrypoint()`** fires. Calls `await ctx.connect()`,
   then `resolve_caller(ctx)` to read SIP participant attributes.
6. Agent starts its session (`AgentSession.start`) and the model begins
   replying in audio.
7. Function-tool calls during the session are regular Python async
   methods — time, weather, Musubi memory reads/writes, and (for the
   household agents) a read-only `household_status` survey. There is no
   delegation: the agent does all of its own work on the call.

## How agents run

The agents run as Docker containers on host `mizuki.mey.house`, defined
in `docker-compose.agents.yaml` (services `agent-aoi`, `agent-nyla`,
`agent-yua`, `agent-sumi`; containers `voice-agent-<name>`). All four
share one image, `voice-agent:latest`, built from `Dockerfile.agent`.
`scripts/agent-entrypoint.sh` selects the per-agent Musubi token, sets
`VOICE_AGENT_NAME`, and execs `agents/<name>/src/agent.py`. Docker
handles restart-on-crash (`restart: unless-stopped`); logs land under
`./logs/voice/` (bind-mounted, `LIVEKIT_VOICE_LOGS`).

The compose split is deliberate: `docker-compose.yaml` is the
**infrastructure tier** (livekit-server, livekit-sip, livekit-egress,
redis — stateless, pinned images), and `docker-compose.agents.yaml` is
the **application tier**. Bring the whole thing up with both files:

```bash
docker compose -f docker-compose.yaml -f docker-compose.agents.yaml up -d
```

## Hardening direction

Current state is "infrastructure as code for the SIP layer" (config in
`config/*.json`, registration via `make register-sip`). Next layers:

- **Drift detection** — a cron that diffs live Redis state against
  `config/*.json` and alerts on mismatch. Catches the class of bug where
  someone `lk sip` CLI's their way around the checked-in config.
- **CI** — pytest on PR, shellcheck for scripts, JSON schema check for
  the config examples.
- **Secrets rotation** — `secrets/livekit-agents.env` is the single
  file. A rotation workflow could update it + re-run `make cycle`
  (which rebuilds the image and recreates the agent containers).
- **Alerting** — `scripts/health-check.sh --json` is already cron-runnable;
  wire to a Discord webhook on `failed > 0`.
