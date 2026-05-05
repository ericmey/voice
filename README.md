# openclaw-livekit

Monorepo for the OpenClaw voice stack: SIP trunking, realtime voice agents, and
the operations layer that wires them together.

## What's in here

```
openclaw-livekit/
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
    └── party/                     chained STT/LLM/TTS variant
```

Every Python package is a uv workspace member declared in the root
`pyproject.toml`. One `.venv/` at the root serves them all.

## Quickstart

```bash
git clone <repo-url> openclaw-livekit
cd openclaw-livekit

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
| `make cycle` | Gracefully restart all three agents in place |
| `make register-sip` | Idempotent SIP trunk + dispatch rule refresh |
| `make health` | Exit-nonzero if any component is unhealthy |
| `make tail` | Follow all three agent logs with color prefix |
| `make truncate-logs` | Clean baseline for a test session |
| `make test` | pytest across all workspace members |
| `make lint` | ruff check + format check |
| `make typecheck` | pyright |

## Architecture & operations

- **[AGENTS.md](AGENTS.md)** — generic agent runbook (Python monorepo conventions, deploy/test flow)
- **[docs/ARCHITECTURE.md](docs/ARCHITECTURE.md)** — how the pieces fit
- **[docs/OPERATIONS.md](docs/OPERATIONS.md)** — deploy / cycle / debug runbook
- **[docs/DISPATCH-RULE-GOTCHAS.md](docs/DISPATCH-RULE-GOTCHAS.md)** — the `numbers` vs `inbound_numbers` trap
- **[tools/README.md](tools/README.md)** — tool catalog

## Status

Personal project shared publicly for reference. Real deployment requires
your own LiveKit, SIP, provider API, Discord/OpenClaw, Musubi, and OTLP
configuration.

## License

MIT — see [LICENSE](LICENSE).
