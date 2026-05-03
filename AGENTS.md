# AGENTS.md — runbook for AI coding agents in this monorepo

Generic technical runbook for any AI coding agent (Claude Code, Codex,
Cursor, Aider, etc.) working in the `openclaw-livekit` monorepo. For
Claude-specific identity / working-style guidance see [CLAUDE.md](CLAUDE.md).

## Continuous learning (read this first)

[docs/AGENT-LESSONS.md](docs/AGENT-LESSONS.md) is the persistent memory
across agent sessions for this repo. Read it at the start of any
non-trivial task. When you encounter a pattern worth remembering — a
trap you fell into, a rule the user gave you, a third-party contract
that surprised you — append a dated entry. Append-only; do not edit
prior entries.

## Monorepo layout

```
openclaw-livekit/
├── pyproject.toml                   uv workspace root + ruff/pyright config
├── docker-compose.yaml              livekit-server + livekit-sip + redis
├── config/                          live configs (bootstrap drops .example → real here)
├── secrets/                         local-only secrets (gitignored)
├── logs/                            runtime voice logs (gitignored)
├── scripts/                         ops verbs (deploy, cycle, health, etc.)
├── docs/                            architecture, operations, gotchas
├── Makefile                         stable wrapper around scripts/
│
├── sdk/                             shared runtime (workspace member)
│   └── src/sdk/                     telemetry, trace, transcript, post-call, config, clients
├── tools/                           @function_tool mixins (workspace member)
│   └── src/tools/                   core, memory, sessions, academy
└── agents/                          voice personas (workspace members)
    ├── nyla/                        realtime — Gemini 2.5 Native Audio, voice "Leda"
    ├── aoi/                         realtime — Gemini 2.5 Native Audio, technical partner
    └── party/                       chained STT/LLM/TTS — Whisper + Silero + Gemini + ElevenLabs
```

Every Python package is a uv workspace member declared in the root
`pyproject.toml`. One `.venv/` at the root serves them all — edits to
any member are live in every other member's imports on the next `uv sync`.

## Pinned runtime

- Python **3.12.13** (see each member's `.python-version`)
- `uv` ≥ 0.5 for workspace + lockfile operations

## Standard verbs (from repo root)

Always prefer `make <verb>` over running scripts directly — the Makefile
is the stable public surface.

```
make help                 # list all verbs

# setup
make bootstrap            # first-time machine setup (deps, configs, root venv)
make sync-venvs           # re-sync root workspace venv (sdk + tools + all agents)

# infrastructure tier (docker compose)
make up / make down       # livekit-server + livekit-sip + redis
make logs                 # docker compose logs -f

# agent tier (launchd)
make deploy               # render launchd plists, install, kickstart agents
make cycle                # kickstart all three agents (picks up code changes)
make teardown             # bootout agents, remove plists

# SIP routing
make register-sip         # idempotent trunk + dispatch rule refresh

# observability
make health               # exit non-zero if any component is unhealthy
make tail                 # follow all three agent logs (color-coded)
make truncate-logs        # zero out agent logs for a clean test baseline

# static checks (pre-release gate)
make lint                 # ruff check + format check
make typecheck            # pyright
make test                 # pytest across all workspace members
make verify               # lint + typecheck + test — run this BEFORE human testing
```

## Rebuilding the workspace venv

```bash
uv sync                   # from the repo root
```

That installs `sdk`, `tools`, and every agent as editable workspace
members into a single `.venv/` at the root. No per-subproject venvs.

After a code change that launchd agents are running, cycle them:

```bash
make cycle                         # all three
scripts/cycle-agents.sh <name>     # one
```

Then run a real phone test — venv changes are not proven until a call
lands transcripts, telemetry, and a call-review in `$LIVEKIT_VOICE_LOGS`
(default `./logs/voice/`).

## Adding a dependency

Workspace-wide (shared across every member):

```bash
uv add <package>
```

Single member (e.g., only the party agent needs a new codec):

```bash
uv add --package agent-party <package>
```

## Adding a new tool

1. Drop a new module into `tools/src/tools/` (e.g., `weather.py`) or
   extend an existing mixin with a new `@function_tool`-decorated method.
2. Add a row to [tools/README.md](tools/README.md) — the catalog.
3. Agents that want the tool add the mixin to their `__mro__` in
   `_shared.py`.

## Static-checks config

Both tools are configured in the root `pyproject.toml`:

- **ruff** — line length 100, rules `E,F,W,I,UP,B`, per-file ignores for
  `test_imports.py` (F401 is the point of those tests).
- **pyright** — basic mode, workspace-wide, with per-agent
  `executionEnvironments` so each agent's `src/` sees its own `_shared.py`
  cleanly (without collisions from the other agents' `_shared.py`).

## Never

- Do not commit real `config/*.yaml`, `config/sip-*.json`, `secrets/*`,
  or `logs/` — they are gitignored.
- Do not revive per-subproject `.venv/` directories. The workspace lives
  at the root.
- Do not add ad-hoc `pip install` calls into the root venv. Update the
  relevant member's `pyproject.toml`, run `uv sync`.
- Do not vendor `sdk` or `tools` into an agent. Workspace references via
  `[tool.uv.sources] sdk = { workspace = true }` are the contract.

## Environment variables each agent reads

The deploy script writes these into each agent's launchd plist from
`secrets/livekit-agents.env`.

| Variable | Who needs it | What it is |
|---|---|---|
| `LIVEKIT_URL` | all | WebSocket URL of the LiveKit server |
| `LIVEKIT_API_KEY`, `LIVEKIT_API_SECRET` | all | Must match `config/livekit.yaml` keys |
| `GOOGLE_API_KEY` | nyla, aoi, party | Gemini API access |
| `OPENAI_API_KEY` | party | Whisper STT |
| `ELEVEN_API_KEY` (alias `ELEVENLABS_API_KEY`) | party | ElevenLabs TTS |
| `GATEWAY_AUTH_TOKEN`, `GATEWAY_PORT` | all | Access to the OpenClaw gateway (memory, sessions) |
| `DISCORD_BOT_TOKEN` | all | Per-agent Discord identity (deploy script maps `DISCORD_TOKEN_<AGENT>` → this) |
| `LIVEKIT_VOICE_LOGS` | all | Directory for voice logs / telemetry / transcripts |
| `OPENCLAW_BIN` | all | Absolute path to the `openclaw` CLI binary (for tool fire-and-forget) |
| `OPENCLAW_OTEL_ENABLED` | all | Enables SigNoz / OTel export |
| `OPENCLAW_OTLP_ENDPOINT`, `OPENCLAW_OTLP_HEADERS` | all | OTLP traces endpoint + optional auth headers |
| `OPENCLAW_OTEL_LOGS_ENABLED`, `OPENCLAW_OTLP_LOGS_ENDPOINT`, `OPENCLAW_OTLP_LOGS_HEADERS` | all | Optional OTLP logs overrides |
| `OPENCLAW_OTEL_METRICS_ENABLED`, `OPENCLAW_OTLP_METRICS_ENDPOINT`, `OPENCLAW_OTLP_METRICS_HEADERS` | all | Optional OTLP metrics overrides |
| `OPENCLAW_RECORD_AUDIO` | all | Enables LiveKit Egress audio recording links |

## References

- [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) — how the components fit
- [docs/OPERATIONS.md](docs/OPERATIONS.md) — deploy / cycle / debug runbook
- [docs/DISPATCH-RULE-GOTCHAS.md](docs/DISPATCH-RULE-GOTCHAS.md) — SIP routing trap
- [tools/README.md](tools/README.md) — tool catalog (what each tool does, args, owners)
- [LiveKit Agents SDK](https://docs.livekit.io/agents/)
- [Gemini Live API](https://ai.google.dev/gemini-api/docs/live)
