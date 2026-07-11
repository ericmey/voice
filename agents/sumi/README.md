# agents/sumi

Sumi Tachibana's voice line — a fully-local chained pipeline on LiveKit.
Registers as `phone-sumi`. Every leg runs on mizuki's Blackwell (sm_120)
card; nothing leaves the box.

Unlike the realtime Aoi / Nyla agents, Sumi composes discrete components:

- **STT:** NVIDIA Riva Parakeet ASR (gRPC, `10.0.20.25:50051`)
- **VAD:** Silero (segments caller audio into utterances)
- **LLM:** Mistral Nemo via llama.cpp (`10.0.20.25:8090`, OpenAI-compatible)
- **TTS:** Orpheus (`10.0.20.25:5005`, OpenAI-compatible; voice `tara` is a
  placeholder until Sumi's own low/dry voice is cloned)

Her persona is Sumi the archivist/maid — composed, dry, care-through-action.
Her voice memory (`sumi/voice`) is a distinct channel from her fleet
presence (`sumi/hermes`); one Sumi, two channels.

## Requirements

- Python **3.12.13** (pinned in `.python-version`)
- A LiveKit server reachable at `LIVEKIT_URL`
- The Sumi inference stack reachable on mizuki (Riva ASR, llama.cpp/Nemo,
  Orpheus) — no cloud STT/LLM/TTS keys needed
- `MUSUBI_V2_TOKEN_SUMI` for the `sumi/voice` memory namespace
- The workspace SDK at [`../../sdk`](../../sdk), resolved through
  `[tool.uv.sources]`

## Running

```bash
uv run --package agent-sumi python agents/sumi/src/agent.py dev
uv run --package agent-sumi python agents/sumi/src/agent.py start
```

See [../../AGENTS.md](../../AGENTS.md) for setup details and the required
environment variables.

## Workspace packages

- [`../../sdk`](../../sdk) — shared runtime
- [`../aoi`](../aoi) — realtime persona
- [`../nyla`](../nyla) — realtime persona

## License

MIT — see [../../LICENSE](../../LICENSE).
