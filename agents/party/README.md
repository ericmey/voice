# openclaw-livekit-agent-party

Party voice agent — a chained-pipeline baseline for multi-agent room
experiments on LiveKit. Registers as `phone-party`.

Unlike the realtime Aoi / Nyla agents, Party composes discrete components:

- **STT:** OpenAI Whisper-1
- **VAD:** Silero (segments caller audio into utterances)
- **LLM:** Gemini 3.1 Flash-Lite Preview (text)
- **TTS:** ElevenLabs `eleven_flash_v2_5`

The goal is to evolve this into a multi-participant room where several
characters can share a call.

## Status

Personal project — shared publicly for reference and portfolio. Not
actively maintained as an open-source community project; issues and PRs
may not be reviewed.

## Requirements

- Python ≥ 3.11, < 3.13
- A LiveKit server reachable at `LIVEKIT_URL`
- Google API key, OpenAI API key, ElevenLabs API key
- The sibling SDK at [`../openclaw-livekit-agent-sdk`](../openclaw-livekit-agent-sdk)
  installed in the active venv

## Running

```bash
python src/agent.py dev    # development
python src/agent.py start  # production
```

See [../AGENTS.md](../AGENTS.md) for setup details and the required
environment variables.

## Sibling subprojects (same monorepo)

- [`../openclaw-livekit-agent-sdk`](../openclaw-livekit-agent-sdk) — shared runtime
- [`../openclaw-livekit-agent-aoi`](../openclaw-livekit-agent-aoi) — realtime persona
- [`../openclaw-livekit-agent-nyla`](../openclaw-livekit-agent-nyla) — realtime persona

## License

MIT — see [../LICENSE](../LICENSE).
