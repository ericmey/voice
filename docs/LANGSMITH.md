# LangSmith Operations

This repo sends LiveKit voice-agent observability to LangSmith through two
paths:

1. **LiveKit OTel spans**: `sdk.tracing.setup_langsmith_tracing()` installs a
   provider before each `AgentServer` is created. LiveKit framework spans then
   flow through `LangSmithSpanProcessor` to the OTLP endpoint.
2. **Curated live runs**: `sdk.telemetry` and `sdk.transcript` emit short-lived
   root runs for transcript messages, tool calls, usage, and errors. These are
   intentionally detached from the long `agent_session` span so they appear in
   LangSmith while the call is still active.

Both paths set `langsmith.metadata.thread_id` to the stable call id when
available. In LangSmith, the `Threads` tab should therefore show one thread per
call, while the `Runs` tab shows the individual transcript/tool/usage actions.

## Startup Order

The order matters:

1. Agent module calls `load_env()` / `load_env_once()`.
2. Env loading calls `setup_langsmith_tracing()`.
3. The module creates `AgentServer`.
4. The job entrypoint registers `wire_langsmith_shutdown_flush(ctx)`.
5. The job awaits `session.start(...)`.
6. The job calls `attach_current_span_metadata(...)`.

This is correct for the pinned LiveKit Agents API with `capture_run=False`:
`AgentSession.start()` creates and attaches the `agent_session` span, starts
runtime tasks, and returns. Re-check this assumption when upgrading LiveKit.

## Environment

Tracing is off unless `LANGSMITH_TRACING=true`.

Required when enabled:

- `OTEL_EXPORTER_OTLP_ENDPOINT=https://api.smith.langchain.com/otel`
- `OTEL_EXPORTER_OTLP_HEADERS=x-api-key=<key>,Langsmith-Project=<fallback>`

Deploy renders per-agent project names:

- `LANGSMITH_PROJECT_NYLA=Nyla`
- `LANGSMITH_PROJECT_AOI=Aoi`
- `LANGSMITH_PROJECT_PARTY=Party`

The tracing setup rewrites the `Langsmith-Project` OTLP header before the
exporter is constructed, so each launchd agent lands in its own LangSmith
project.

Useful toggles:

- `LANGSMITH_VERBOSE_TELEMETRY=true`: export low-level state/metric chatter
  such as speaking state changes, overlap events, and raw realtime metrics.
- `LANGSMITH_PROCESSOR_DEBUG=true`: print span extraction diagnostics.
- `LANGSMITH_ATTACH_AUDIO=true`: start LiveKit Egress and attach full-call
  audio to LangSmith after hangup.

## Two-Track Runs

Some events appear twice by design:

- LiveKit nested spans preserve the framework execution tree.
- Curated root runs make operator views live, filterable, and thread-friendly.

For example, a tool call can appear as a nested LiveKit `function_tool` span and
as an immediate top-level `tool` run. The nested span is useful for turn
hierarchy and evaluator context; the curated run is useful for live monitoring
and filtering by `tool:<name>`.

## Audio Attachments

Full-call audio uses LiveKit Egress, not OTel. The agent starts an audio-only
room composite recording, waits for the output file on shutdown, then uploads a
LangSmith SDK `RunTree` named `call_audio` with a `full_call_audio` attachment.

Local compose mounts:

- Host: `./logs/voice/recordings`
- Egress container: `/recordings`

The `call_audio` run carries the same `thread_id` / `call_sid` metadata as the
rest of the call.

## Verification

Run:

```bash
make trace-check
make langsmith-plan
make langsmith-provision
make up
make deploy
```

Then place a real call and verify:

- The run lands in the correct per-agent project.
- One LangSmith thread represents the call.
- Transcript, tool, and usage rows appear while the call is live.
- A `call_audio` run with `full_call_audio` appears after hangup.
