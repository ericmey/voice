# AGENTS.md — runbook for AI coding agents in this monorepo

Generic technical runbook for any AI coding agent (Claude Code, Codex,
Cursor, Aider, etc.) working in the `voice` monorepo. For
Claude-specific repo notes see [CLAUDE.md](CLAUDE.md).

## Continuous learning (read this first)

[docs/AGENT-LESSONS.md](docs/AGENT-LESSONS.md) is the persistent memory
across agent sessions for this repo. Read it at the start of any
non-trivial task. When you encounter a pattern worth remembering — a
trap you fell into, a rule the user gave you, a third-party contract
that surprised you — append a dated entry. Append-only; do not edit
prior entries.

## Monorepo layout

```
voice/
├── pyproject.toml                   uv workspace root + ruff/pyright config
├── docker-compose.yaml              livekit-server + livekit-sip + livekit-egress + redis
├── docker-compose.agents.yaml       the four voice-agent containers
├── Dockerfile.agent                 builds voice-agent:latest (shared by all agents)
├── config/                          live configs (bootstrap drops .example → real here)
├── secrets/                         local-only secrets (gitignored)
├── logs/                            runtime voice logs (gitignored)
├── scripts/                         ops verbs (bootstrap, health, sip, tail, etc.)
├── docs/                            architecture, operations, gotchas
├── Makefile                         stable wrapper around scripts/ + docker compose
│
├── sdk/                             shared runtime (workspace member)
│   └── src/sdk/                     telemetry, trace, transcript, post-call, config, clients
├── tools/                           @function_tool mixins (workspace member)
│   └── src/tools/                   core, memory, household
└── agents/                          voice personas (workspace members)
    ├── nyla/                        realtime — Gemini 2.5 Native Audio, voice "Aoede"
    ├── aoi/                         realtime — Gemini 2.5 Native Audio, technical partner
    ├── yua/                         realtime — Gemini 2.5 Native Audio, coding and QA partner
    └── sumi/                        chained STT/LLM/TTS — Riva ASR + Silero + Mistral Nemo + Orpheus (fully local)
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
make up / make down       # livekit-server + livekit-sip + livekit-egress + redis
make logs                 # docker compose logs -f

# agent tier (docker compose)
make deploy               # build voice-agent:latest, bring up infra + the four agents
make cycle                # rebuild the image + recreate agent containers (picks up code changes)

# SIP routing
make register-sip         # idempotent trunk + dispatch rule refresh

# observability
make health               # exit non-zero if any component is unhealthy
make tail                 # follow all agent logs (color-coded)
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
The root venv is for local lint/typecheck/test; the running agents use
the `voice-agent:latest` image built from `Dockerfile.agent`.

After a code change, rebuild the image and recreate the agent containers:

```bash
make cycle                         # rebuild image + recreate all agent containers
# one agent: rebuild the shared image, then recreate just that service
docker build -f Dockerfile.agent -t voice-agent:latest .
docker compose -f docker-compose.yaml -f docker-compose.agents.yaml up -d agent-nyla
```

Then run a real phone test — code changes are not proven until a call
lands transcripts, telemetry, and a call-review in `$LIVEKIT_VOICE_LOGS`
(default `./logs/voice/`).

## Adding a dependency

Workspace-wide (shared across every member):

```bash
uv add <package>
```

Single member (e.g., only the sumi agent needs a new codec):

```bash
uv add --package agent-sumi <package>
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

Each agent container loads these from `secrets/livekit-agents.env`
(`env_file` in `docker-compose.agents.yaml`).
`scripts/agent-entrypoint.sh` maps the per-agent `MUSUBI_V2_TOKEN_<AGENT>`
to the unsuffixed `MUSUBI_V2_TOKEN` the SDK reads, and exports
`VOICE_AGENT_NAME` (which is what makes `SERVICE_NAME` per-agent).

| Variable | Who needs it | What it is |
|---|---|---|
| `LIVEKIT_URL` | all | WebSocket URL of the LiveKit server (`ws://livekit-server:7880` in compose) |
| `LIVEKIT_API_KEY`, `LIVEKIT_API_SECRET` | all | Must match `config/livekit.yaml` keys |
| `GOOGLE_API_KEY` | nyla, aoi, yua | Gemini API access (realtime agents) |
| `MUSUBI_V2_TOKEN_SUMI` | sumi | `sumi/voice` memory bearer (no cloud STT/LLM/TTS keys — fully local) |
| `MUSUBI_V2_BASE_URL` | all | Canonical Musubi API base URL |
| `MUSUBI_V2_TOKEN_<AGENT>` | all | Per-agent Musubi bearer token; entrypoint maps it to `MUSUBI_V2_TOKEN` |
| `VOICE_AGENT_NAME` | all | Agent id; set by `agent-entrypoint.sh`, drives `SERVICE_NAME=voice-<agent>` |
| `LIVEKIT_VOICE_LOGS` | all | Directory for voice logs / telemetry / transcripts |
| `VOICE_OTEL_ENABLED` | all | Enables OTel export |
| `VOICE_OTLP_ENDPOINT`, `VOICE_OTLP_HEADERS` | all | OTLP traces endpoint + optional auth headers |
| `VOICE_OTEL_LOGS_ENABLED`, `VOICE_OTLP_LOGS_ENDPOINT`, `VOICE_OTLP_LOGS_HEADERS` | all | Optional OTLP logs overrides |
| `VOICE_OTEL_METRICS_ENABLED`, `VOICE_OTLP_METRICS_ENDPOINT`, `VOICE_OTLP_METRICS_HEADERS` | all | Optional OTLP metrics overrides |
| `VOICE_RECORD_AUDIO` | all | Enables LiveKit Egress audio recording links |

## References

- [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) — how the components fit
- [docs/OPERATIONS.md](docs/OPERATIONS.md) — deploy / cycle / debug runbook
- [docs/DISPATCH-RULE-GOTCHAS.md](docs/DISPATCH-RULE-GOTCHAS.md) — SIP routing trap
- [tools/README.md](tools/README.md) — tool catalog (what each tool does, args, owners)
- [LiveKit Agents SDK](https://docs.livekit.io/agents/)
- [Gemini Live API](https://ai.google.dev/gemini-api/docs/live)
