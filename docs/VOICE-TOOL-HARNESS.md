# Voice Tool Harness

Use the voice tool harness to test phone-agent OpenClaw delegation without
placing a live call. It instantiates the real Nyla, Aoi, or Party agent
class and runs the real delegation method.

## Safe Mock Mode

Mock mode is the default. It patches the Gateway hook client in-process, so
no OpenClaw request is sent.

```bash
make voice-harness
uv run python sdk/scripts/voice_tool_harness.py --agent nyla
uv run python sdk/scripts/voice_tool_harness.py --agent aoi --case ops-check
uv run python sdk/scripts/voice_tool_harness.py --agent party --case selfie
```

The output shows:

- which tools are model-visible;
- which old compatibility helpers are helper-only;
- whether `academy_*` tools are absent;
- the exact `/hooks/agent` request shape that would have been sent.

Use JSON output when wiring the harness into scripts:

```bash
uv run python sdk/scripts/voice_tool_harness.py --agent nyla --json
```

## Custom Case

Use `--agent-id` and `--task` together to test a specific handoff:

```bash
uv run python sdk/scripts/voice_tool_harness.py \
  --agent aoi \
  --agent-id hana \
  --task "Draw a quick concept image"
```

That is useful for allowlist checks. For example, Aoi should reject targets
outside her delegation allowlist before any hook request is attempted.

## Live Hook Smoke

Use `--live-hooks` only when you intentionally want to submit to OpenClaw:

```bash
OPENCLAW_HOOK_TOKEN=... \
OPENCLAW_GATEWAY_HTTP_URL=http://127.0.0.1:18789 \
uv run python sdk/scripts/voice_tool_harness.py \
  --agent nyla \
  --case ops-check \
  --live-hooks
```

Live mode expects OpenClaw Gateway hooks to be enabled and constrained with
`hooks.allowedAgentIds`. The harness waits only for Gateway acceptance; the
target OpenClaw agent completes the work through its normal channels.

## What This Does Not Test

This harness does not test SIP routing, LiveKit room dispatch, audio, VAD,
or model tool-choice behavior. Use the existing LiveKit text simulator in
[sdk/scripts/text_simulator.py](../sdk/scripts/text_simulator.py) when you
need a room-level simulation, and a real phone call only for final audio/SIP
confidence.
