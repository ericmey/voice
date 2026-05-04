# Observability

This repo emits OpenTelemetry spans, logs, and metrics over generic
OTLP/HTTP. It does not require a specific vendor. Point the environment
variables in `secrets/livekit-agents.env` at your own collector, whether
that is a local Grafana LGTM stack, Grafana Cloud, Honeycomb, Datadog,
or another OTLP-compatible backend.

## Agent Wiring

Example local collector configuration:

```bash
OPENCLAW_OTEL_ENABLED=true
OPENCLAW_OTLP_ENDPOINT=http://localhost:4318/v1/traces
OPENCLAW_OTEL_LOGS_ENABLED=true
OPENCLAW_DEPLOYMENT_ENVIRONMENT=local
OPENCLAW_SERVICE_VERSION=dev
```

Then redeploy the launchd agents:

```bash
make deploy
```

`OPENCLAW_OTLP_ENDPOINT` is the traces endpoint. The SDK derives logs
and metrics endpoints automatically when explicit overrides are unset:

- traces: `http://localhost:4318/v1/traces`
- logs: `http://localhost:4318/v1/logs`
- metrics: `http://localhost:4318/v1/metrics`

Use `OPENCLAW_OTLP_HEADERS`, `OPENCLAW_OTLP_LOGS_HEADERS`, and
`OPENCLAW_OTLP_METRICS_HEADERS` for hosted backends that need auth
headers.

## What Emits Data

```
LiveKit agents (Python)
  ├─ OTel TracerProvider ──┬─ NoiseSpanFilter
  │                        └─ BatchSpanProcessor → OTLP/HTTP → traces backend
  ├─ OTel LoggerProvider ──── BatchLogRecordProcessor → OTLP/HTTP → logs backend
  └─ OTel MeterProvider ───── PeriodicExportingMetricReader → OTLP/HTTP → metrics backend
                                  service.name=openclaw-livekit-{nyla,aoi,party}

Optional OpenClaw gateway
  └─ diagnostics-otel plugin or equivalent → same collector
```

The agent SDK relies on LiveKit Agents' native OTel spans and attributes.
Custom enrichment is intentionally minimal:

- `NoiseSpanFilter` drops framework-noise spans such as
  `agent_speaking`, `user_speaking`, `on_enter`, and `on_exit` unless
  `OPENCLAW_OTEL_VERBOSE=true`.
- `attach_current_span_metadata()` stamps call routing identity on the
  active `agent_session` span.

## Host Collector

The repo includes a macOS launchd installer for `otelcol-contrib`:

```bash
make host-collector-install
make host-collector-status
make host-collector-logs
make host-collector-restart
make host-collector-uninstall
```

The rendered collector config can scrape:

| service.name | Pipeline | What it surfaces |
| --- | --- | --- |
| `host-mac` | `metrics/host` | CPU, memory, disk, filesystem, network, process counts |
| container names | `metrics/docker` | Docker CPU, memory, network, block IO |
| `httpcheck` | `metrics/httpcheck` | Unauthenticated dependency reachability checks |
| `openclaw-gateway` | `logs/openclaw` | Gateway file logs, if present |
| `openclaw-livekit-host` | `logs/agents` | Agent log files as a fallback path |

Before installing, edit
[config/otel-collector/config.yaml.template](../config/otel-collector/config.yaml.template)
if your upstream collector is not `http://localhost:4318`.

## Span Identity

### Resource Attributes

| Key | Value |
| --- | --- |
| `service.name` | `openclaw-livekit-<agent>` |
| `service.namespace` | `openclaw` |
| `service.version` | `$OPENCLAW_SERVICE_VERSION` |
| `service.instance.id` | unique per process |
| `deployment.environment` | `$OPENCLAW_DEPLOYMENT_ENVIRONMENT` |
| `host.name` | hostname |
| `process.pid` | OS pid |

### Call Attributes

`attach_current_span_metadata()` adds:

- `session.id` — SIP Call-ID
- `enduser.id` — caller identifier, if available
- `openclaw.dialed_number` — dialed DID, if available
- `openclaw.caller_source` — `sip` or `unknown`
- `openclaw.lk_job_id` — LiveKit job id

## HTTP Instrumentation

When `OPENCLAW_OTEL_HTTP_INSTRUMENTATION` is unset or true, the SDK
auto-instruments:

- `httpx`
- `aiohttp-client`
- `requests`

Outbound calls to memory, gateway, weather, LLM, STT, and TTS providers
become `http.client` spans and feed the `http.client.duration`
histogram.

## Audio Recording

Set `OPENCLAW_RECORD_AUDIO=true` to have LiveKit Egress write each call
to:

```text
${LIVEKIT_EGRESS_HOST_RECORDINGS_DIR}/<agent>/<call_sid>.ogg
```

The active call span gets:

- `openclaw.audio.path`
- `openclaw.audio.mime_type`
- `openclaw.audio.egress_id`, when available
- `openclaw.audio.url`, when `OPENCLAW_AUDIO_PUBLIC_BASE_URL` is set

## Verifying Ingestion

```bash
make health

launchctl print "gui/$(id -u)/ai.openclaw.livekit-agent-nyla" \
  | grep OPENCLAW_OTEL
```

Backend-specific checks depend on your stack. For a local Grafana LGTM
stack, useful checks are typically:

```bash
curl -sS 'http://localhost:9009/prometheus/api/v1/label/service_name/values' | jq

curl -sS -G 'http://localhost:3100/loki/api/v1/query_range' \
  --data-urlencode 'query={service_name="openclaw-livekit-nyla"}' \
  --data-urlencode 'limit=5' \
  --data-urlencode "start=$(($(date +%s) - 300))000000000" | jq
```

Tempo traces are easiest to inspect through Grafana Explore by filtering
for `service.name=openclaw-livekit-<agent>`.

## Archived LangSmith Path

This repo previously carried LangSmith-specific provisioning and custom
span enrichment. The active SDK path is now vendor-neutral OTLP/HTTP.
The archived LangSmith provisioning tree remains under `ops/langsmith/`;
see [LANGSMITH.md](LANGSMITH.md) for reactivation options.
