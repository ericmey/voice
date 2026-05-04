# sdk/

Shared Python runtime for the OpenClaw LiveKit voice agents. Provides the
worker bootstrap, telemetry / trace / transcript writers, tool mixins
(time, weather, memory, sessions, academy), and the post-call review
pipeline that feeds Rin's review queue.

This is the common substrate the persona agents (Aoi, Nyla, Party) all
import from.

## Requirements

- Python **3.12.13** (pinned in `.python-version`)
- `uv` for lockfile operations

## Rebuild

```bash
uv sync
```

Run from the monorepo root. See [../AGENTS.md](../AGENTS.md) for the
full workspace setup, deploy, and verification runbook.

## Workspace packages

- [`../agents/aoi`](../agents/aoi) — Aoi persona (Gemini 2.5 realtime)
- [`../agents/nyla`](../agents/nyla) — Nyla persona (Gemini 2.5 realtime)
- [`../agents/party`](../agents/party) — chained STT/LLM/TTS baseline
- [`../tools`](../tools) — shared LiveKit function-tool mixins

## License

MIT — see [../LICENSE](../LICENSE).
