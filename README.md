# voice

Monorepo for the OpenClaw voice stack: SIP trunking, realtime voice agents, and
the operations layer that wires them together.

## What's in here

```
voice/
├── pyproject.toml                 uv workspace root + ruff/pyright config
├── docker-compose.yaml            livekit-server + livekit-sip + redis
├── config/                        live configs (bootstrap copies .example files here)
├── secrets/                       local-only secrets (gitignored)
├── logs/                          runtime voice logs (gitignored)
├── scripts/                       ops verbs (deploy, cycle, health, etc.)
├── docs/                          architecture, operations, gotchas
├── Makefile                       stable wrapper around scripts/
│
├── sdk/                           shared runtime (telemetry, trace, transcript, post-call, clients)
├── tools/                         @function_tool mixins — browseable catalog at tools/README.md
└── agents/                        voice personas
    ├── nyla/                      realtime persona (Gemini 2.5 native audio)
    ├── aoi/                       realtime persona, technical partner
    ├── yua/                       realtime persona, coding and QA partner
    └── party/                     chained STT/LLM/TTS variant
```

Every Python package is a uv workspace member declared in the root
`pyproject.toml`. One `.venv/` at the root serves them all.

## Bring Your Own Stack

This repo is a working reference for one personal voice stack, not a
drop-in product. The included agents (`nyla`, `aoi`, `yua`, `party`) and tools
are samples you can replace with your own personas, model choices, and
tool mixins.

Before trying to run it, have these pieces at hand:

| Area | What you provide | Where to configure |
|------|------------------|--------------------|
| LiveKit + SIP | LiveKit server/API keys and SIP bridge config | `config/livekit*.yaml`, `docker-compose.yaml` |
| SIP provider | A DID/trunk provider such as Twilio | `config/sip-*.json`, [docs/twilio-trunk.md](docs/twilio-trunk.md) |
| Agents | Personas, prompts, voices, models | `agents/*`, `scripts/deploy-agents.sh` |
| Tools | Function tools the voice model may call | `tools/src/tools/`, [tools/README.md](tools/README.md) |
| OpenClaw | Optional Gateway hook target for async delegation | `VOICE_HOOK_*`, [OpenClaw](https://github.com/openclaw/openclaw) |
| Musubi | Optional memory/presence service | `MUSUBI_V2_*`, [Musubi](https://github.com/ericmey/musubi) |
| Telemetry | Any OTLP/HTTP backend or collector | `VOICE_OTLP_*`, [docs/OBSERVABILITY.md](docs/OBSERVABILITY.md) |
| macOS supervisor | launchd runs the Python agents | `config/launchd/`, `make deploy`, `make cycle` |

See [docs/BRING-YOUR-OWN-STACK.md](docs/BRING-YOUR-OWN-STACK.md) for the
replacement map and external project pointers.

## Config And Secrets

The repo commits example configs only. Real runtime files are local-only
and gitignored:

| Local file | Starts from | Purpose |
|------------|-------------|---------|
| `secrets/livekit-agents.env` | `config/secrets.env.example` | Provider keys, OpenClaw/Musubi/OTLP endpoints, launchd env |
| `config/livekit.yaml` | `config/livekit.yaml.example` | LiveKit server API keys and runtime config |
| `config/livekit-sip.yaml` | `config/livekit-sip.yaml.example` | LiveKit SIP bridge config |
| `config/livekit-egress.yaml` | `config/livekit-egress.yaml.example` | Optional audio recording/egress config |
| `config/sip-*.json` | `config/sip-*.json.example` | SIP trunk and dispatch rules |

`make bootstrap` creates the missing local files from examples where it
can. After editing configs/secrets, use `make up`, `make register-sip`,
and `make deploy` to apply the stack.

## Quickstart

```bash
git clone <repo-url> voice
cd voice

# First time on a new machine
make bootstrap

# Then edit the files bootstrap dropped in config/ and secrets/
# (it'll tell you exactly which ones).

# Bring up infrastructure
brew services stop redis    # compose ships redis; one-time cleanup
make up                     # docker compose up -d
make register-sip           # register trunk + dispatch rules from config
make deploy                 # render plists, install, gracefully restart agents
make verify                 # lint + typecheck + test — green before human testing
make health                 # verify everything is green
```

## Common operational verbs

| Verb | What it does |
|------|--------------|
| `make help` | List all verbs |
| `make verify` | Lint + typecheck + test (run before a real phone call) |
| `make up` / `make down` | Bring the docker-compose stack up/down |
| `make deploy` | Render launchd plists + install/restart agents with LiveKit drain |
| `make teardown` | Bootout agents, remove plists (source stays put) |
| `make cycle` | Gracefully restart all agents in place |
| `make register-sip` | Idempotent SIP trunk + dispatch rule refresh |
| `make health` | Exit-nonzero if any component is unhealthy |
| `make tail` | Follow all agent logs with color prefix |
| `make truncate-logs` | Clean baseline for a test session |
| `make test` | pytest across all workspace members |
| `make lint` | ruff check + format check |
| `make typecheck` | pyright |

## Architecture & operations

- **[AGENTS.md](AGENTS.md)** — generic agent runbook (Python monorepo conventions, deploy/test flow)
- **[docs/ARCHITECTURE.md](docs/ARCHITECTURE.md)** — how the pieces fit
- **[docs/BRING-YOUR-OWN-STACK.md](docs/BRING-YOUR-OWN-STACK.md)** — replacement map for external stacks
- **[docs/OPERATIONS.md](docs/OPERATIONS.md)** — deploy / cycle / debug runbook
- **[docs/DISPATCH-RULE-GOTCHAS.md](docs/DISPATCH-RULE-GOTCHAS.md)** — the `numbers` vs `inbound_numbers` trap
- **[tools/README.md](tools/README.md)** — tool catalog

## Status

Personal project shared publicly for reference. Real deployment requires
your own LiveKit, SIP, provider API, Discord/OpenClaw, Musubi, and OTLP
configuration.

## License

MIT — see [LICENSE](LICENSE).
