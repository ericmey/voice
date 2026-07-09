# Project Status

This file is intentionally public-safe. It tracks repo state without
listing private phone numbers, trunk IDs, local filesystem archives, or
operator-specific infrastructure.

## Current State

`voice` is a monorepo for a local LiveKit voice stack:

- Docker Compose runs the infrastructure tier: `livekit-server`,
  `livekit-sip`, `livekit-egress`, and Redis.
- `launchd` runs the Python agent tier on macOS: `phone-nyla`,
  `phone-aoi`, `phone-yua`, and `phone-party`.
- SIP routing is represented as JSON examples in `config/`; real
  routing files are local-only and gitignored.
- Agent logs, transcripts, per-call JSON telemetry, post-call reviews,
  and optional audio recordings write under `$LIVEKIT_VOICE_LOGS`
  (default `./logs/voice/`).
- Observability is plain OTLP/HTTP. The examples point at placeholder
  collector endpoints; operators should set their own
  `VOICE_OTLP_*` values in `secrets/livekit-agents.env`.

## Verification

Use the root Makefile:

```bash
make lint
make typecheck
make test
make verify
```

Integration tests that talk to live services are opt-in and should stay
guarded by explicit environment variables.

## Known Operational Boundaries

- `schedule_callback` was deleted on 2026-07-09 along with the rest of
  `tools/sessions.py`. It depended on an external CLI gateway that is
  retired. There is no callback scheduling; the prompts say so plainly.
- There is no delegation from the phone. The voice agents answer calls and
  read/write Musubi; they cannot hand work to another agent.
- Agents run as containers on `mizuki.mey.house` via
  `docker-compose.agents.yaml`. The macOS LaunchAgent path is gone.
- The LangSmith provisioning tree is archived, not part of the active
  telemetry path. See [LANGSMITH.md](LANGSMITH.md).

## Local-Only State

Keep these out of git:

- Real SIP trunk and dispatch JSON in `config/`.
- Runtime secrets in `secrets/`.
- Runtime logs, transcripts, recordings, telemetry, and reviews in
  `logs/`.
- Provider account IDs, trunk SIDs, phone numbers, and private hostnames.
