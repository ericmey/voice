# agents/aoi

Aoi voice agent — a realtime voice persona built on the LiveKit Agents SDK
and Google's Gemini 2.5 Flash Native Audio API. Registers as `phone-aoi`
with LiveKit; the livekit-sip container (bind-mounted config at
`../../config/livekit-sip.yaml`) routes inbound PSTN calls to it via dispatch
rule.

## Requirements

- Python **3.12.13** (pinned in `.python-version`)
- A LiveKit server reachable at `LIVEKIT_URL`
- Google API key for Gemini
- The workspace SDK at [`../../sdk`](../../sdk), resolved through
  `[tool.uv.sources]`

## Running

```bash
uv run --package agent-aoi python agents/aoi/src/agent.py dev
uv run --package agent-aoi python agents/aoi/src/agent.py start
```

See [../../AGENTS.md](../../AGENTS.md) for rebuild procedures, launchd
integration, and the required environment variables.

## Workspace packages

- [`../../sdk`](../../sdk) — shared runtime
- [`../nyla`](../nyla) — sister persona
- [`../sumi`](../sumi) — chained-pipeline variant

## License

MIT — see [../../LICENSE](../../LICENSE).
