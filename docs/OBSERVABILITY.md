# Observability

This repo emits OpenTelemetry spans, logs, and metrics over generic
OTLP/HTTP. It does not require a specific vendor. Point the environment
variables in `secrets/livekit-agents.env` at your own collector, whether
that is a local Grafana LGTM stack, Grafana Cloud, Honeycomb, Datadog,
or another OTLP-compatible backend.

Useful external references:

- [OpenTelemetry Collector](https://opentelemetry.io/docs/collector/)
- [OTLP protocol](https://opentelemetry.io/docs/specs/otlp/)
- [Grafana Cloud OTLP](https://grafana.com/docs/grafana-cloud/send-data/otlp/)
- [Grafana OpenTelemetry Collector setup](https://grafana.com/docs/opentelemetry/collector/opentelemetry-collector/)

## Agent Wiring

Example local collector configuration:

```bash
VOICE_OTEL_ENABLED=true
VOICE_OTLP_ENDPOINT=http://localhost:4318/v1/traces
VOICE_OTEL_LOGS_ENABLED=true
VOICE_DEPLOYMENT_ENVIRONMENT=local
VOICE_SERVICE_VERSION=dev  # use a release, image tag, or git SHA in deploys
```

Then recreate the agent containers so they pick up the new env:

```bash
make cycle
```

`VOICE_OTLP_ENDPOINT` is the traces endpoint. The SDK derives logs
and metrics endpoints automatically when explicit overrides are unset:

- traces: `http://localhost:4318/v1/traces`
- logs: `http://localhost:4318/v1/logs`
- metrics: `http://localhost:4318/v1/metrics`

Use `VOICE_OTLP_HEADERS`, `VOICE_OTLP_LOGS_HEADERS`, and
`VOICE_OTLP_METRICS_HEADERS` for hosted backends that need auth
headers.

## What Emits Data

```
LiveKit agents (Python)
  ├─ OTel TracerProvider ──┬─ NoiseSpanFilter
  │                        └─ BatchSpanProcessor → OTLP/HTTP → traces backend
  ├─ OTel LoggerProvider ──── BatchLogRecordProcessor → OTLP/HTTP → logs backend
  └─ OTel MeterProvider ───── PeriodicExportingMetricReader → OTLP/HTTP → metrics backend
                                  service.name=voice-{nyla,aoi,yua,sumi}
```

The agent SDK relies on LiveKit Agents' native OTel spans and attributes.
Custom enrichment is intentionally minimal:

- `NoiseSpanFilter` drops framework-noise spans such as
  `agent_speaking`, `user_speaking`, `on_enter`, and `on_exit` unless
  `VOICE_OTEL_VERBOSE=true`.
- `attach_current_span_metadata()` stamps call routing identity on the
  active `agent_session` span.

## Collector

The agents export OTLP/HTTP directly to an external OpenTelemetry
collector — in this deployment, `shiori.mey.house:4318`. There is no
in-repo collector: point `VOICE_OTLP_ENDPOINT` (and the optional
per-signal overrides) at whatever collector or hosted OTLP backend you
run. See "Agent Wiring" above for the variables.

## Span Identity

### Resource Attributes

| Key | Value |
| --- | --- |
| `service.name` | `voice-<agent>` |
| `service.namespace` | `voice` |
| `service.version` | `$VOICE_SERVICE_VERSION` or deploy-time git SHA |
| `service.instance.id` | unique per process |
| `deployment.environment` | `$VOICE_DEPLOYMENT_ENVIRONMENT` |
| `host.name` | hostname |
| `process.pid` | OS pid |

### Call Attributes

`attach_current_span_metadata()` adds:

- `session.id` — SIP Call-ID
- `enduser.id` — caller identifier, if available
- `voice.dialed_number` — dialed DID, if available
- `voice.caller_source` — `sip` or `unknown`
- `voice.lk_job_id` — LiveKit job id

## HTTP Instrumentation

When `VOICE_OTEL_HTTP_INSTRUMENTATION` is unset or true, the SDK
auto-instruments:

- `httpx`
- `aiohttp-client`
- `requests`

Outbound calls to memory, weather, LLM, STT, and TTS providers become
`http.client` spans and feed the `http.client.duration` histogram.

## Audio Recording

Set `VOICE_RECORD_AUDIO=true` to have LiveKit Egress write each call
to:

```text
${LIVEKIT_EGRESS_HOST_RECORDINGS_DIR}/<agent>/<call_sid>.ogg
```

The active call span gets:

- `voice.audio.path`
- `voice.audio.mime_type`
- `voice.audio.egress_id`, when available
- `voice.audio.url`, when `VOICE_AUDIO_PUBLIC_BASE_URL` is set

## Verifying Ingestion

```bash
make health

docker exec voice-agent-nyla env | grep VOICE_OTEL
```

Backend-specific checks depend on your stack. For a local Grafana LGTM
stack, useful checks are typically:

```bash
curl -sS 'http://localhost:9009/prometheus/api/v1/label/service_name/values' | jq

curl -sS -G 'http://localhost:3100/loki/api/v1/query_range' \
  --data-urlencode 'query={service_name="voice-nyla"}' \
  --data-urlencode 'limit=5' \
  --data-urlencode "start=$(($(date +%s) - 300))000000000" | jq
```

Tempo traces are easiest to inspect through Grafana Explore by filtering
for `service.name=voice-<agent>`.

## Archived LangSmith Path

This repo previously carried LangSmith-specific provisioning and custom
span enrichment. The active SDK path is now vendor-neutral OTLP/HTTP.
The archived LangSmith provisioning tree remains under `ops/langsmith/`;
the LangSmith path is retired and LANGSMITH.md was deleted with it. Observability now runs through OTLP to the collector on shiori.
