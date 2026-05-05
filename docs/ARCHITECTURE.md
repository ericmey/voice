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
                      │  Python voice agents          │  launchd managed
                      │  (nyla, aoi, party)           │  host-native venvs
                      │                               │
                      │  Each exposes @function_tool  │
                      │  methods for memory, OpenClaw │
                      │  delegation, time, weather    │
                      └───────────────────────────────┘
```

## Workspace Packages

### `sdk/`
Shared Python package imported by the three agents. Contains:

- **`config.py`** — `AgentConfig` dataclass (agent_name, memory_agent_tag,
  discord_room, allowed_delegation_targets). Per-agent operational identity;
  the mixin stack reads `self.config.*` instead of hardcoded constants.
- **`tools/`** — Core, Memory, and OpenClaw delegation mixins that each agent
  inherits. Function-tool decorated methods expose capabilities to the
  voice model.
- **`telephony.py`** — `resolve_caller()` reads the SIP participant's
  attributes from a connected room. One hop behind the agent entrypoint.
- **`openclaw_hooks.py`** — async Gateway `/hooks/agent` client used by
  voice tools to hand work to the real OpenClaw agents without shelling
  out or owning downstream channel routing.
- **`cli_spawner.py`** — legacy detached subprocess helper retained for
  disabled callback code while that path is redesigned.
- **`musubi_v2_client.py`** — async HTTP client for the canonical Musubi API.
- **`trace.py`**, **`transcript.py`**, **`env.py`** — ancillary.

### `agents/nyla/`
Realtime voice persona. Gemini 2.5 Flash Native Audio, Leda voice. Registers
as `phone-nyla`. Household router — no delegation restrictions.

### `agents/aoi/`
Realtime voice persona. Same model as Nyla, Kore voice, distinct prompt.
Tighter delegation allowlist (`{yumi, rin, aoi, nyla}`) — technical
partner, not household router.

### `agents/party/`
Chained STT/LLM/TTS variant. Whisper → Silero VAD → Gemini text LLM →
ElevenLabs TTS. Same persona/tools as Nyla; different voice engine for A/B.

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
   methods. Delegation posts to OpenClaw Gateway `/hooks/agent` and
   returns once the Gateway accepts the request; the target OpenClaw
   agent owns downstream tools, skills, and channel delivery.

## Why agents aren't in docker-compose

Launchd is handling agent lifecycle today — auto-restart on crash,
environment injection from a secrets file, log rotation to a known path
under `./logs/voice/` (or wherever `LIVEKIT_VOICE_LOGS` points).
Dropping Python agents into Docker on
macOS adds networking complexity (host mode for WebSocket to livekit-server
would work, but `uv sync` + hot reload + venv caching get friction-ful).

Mental model: **compose is for the infrastructure tier** (stateless,
pinned images); **launchd is for the application tier** (host-native
Python, frequent code changes). If we ever move off Mac, agents can
migrate to a container or systemd unit without touching the compose file.

## Hardening direction

Current state is "infrastructure as code for the SIP layer" (config in
`config/*.json`, registration via `make register-sip`). Next layers:

- **Drift detection** — a cron that diffs live Redis state against
  `config/*.json` and alerts on mismatch. Catches the class of bug where
  someone `lk sip` CLI's their way around the checked-in config.
- **CI** — pytest on PR, shellcheck for scripts, JSON schema check for
  the config examples.
- **Secrets rotation** — `secrets/livekit-agents.env` is the single
  file. A rotation workflow could update it + re-run
  `scripts/deploy-agents.sh` (which re-renders plists and gracefully
  restarts agents).
- **Alerting** — `scripts/health-check.sh --json` is already cron-runnable;
  wire to a Discord webhook on `failed > 0`.
