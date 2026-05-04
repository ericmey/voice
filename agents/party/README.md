# agents/party

Party voice agent — a chained-pipeline baseline for multi-agent room
experiments on LiveKit. Registers as `phone-party`.

Unlike the realtime Aoi / Nyla agents, Party composes discrete components:

- **STT:** OpenAI Whisper-1
- **VAD:** Silero (segments caller audio into utterances)
- **LLM:** Gemini text model configured in `src/agent.py`
- **TTS:** ElevenLabs `eleven_flash_v2_5`

The goal is to evolve this into a multi-participant room where several
characters can share a call.

## Requirements

- Python **3.12.13** (pinned in `.python-version`)
- A LiveKit server reachable at `LIVEKIT_URL`
- Google API key, OpenAI API key, ElevenLabs API key
- The workspace SDK at [`../../sdk`](../../sdk), resolved through
  `[tool.uv.sources]`

## Running

```bash
uv run --package agent-party python agents/party/src/agent.py dev
uv run --package agent-party python agents/party/src/agent.py start
```

See [../../AGENTS.md](../../AGENTS.md) for setup details and the required
environment variables.

## Workspace packages

- [`../../sdk`](../../sdk) — shared runtime
- [`../aoi`](../aoi) — realtime persona
- [`../nyla`](../nyla) — realtime persona

## License

MIT — see [../../LICENSE](../../LICENSE).
