# Observability — shiori LGTM stack

This repo emits **OpenTelemetry-first** spans, logs, and metrics and
ships them to the fleet's primary observability backend: a self-hosted
**LGTM stack** (Grafana + Loki + Tempo + Mimir behind an OTel
Collector) running on `shiori.mey.house`. Grafana is the call-narrative
view, the slow-tool microscope (Tempo), the log explorer (Loki), the
metrics dashboard (Mimir), and the service map — all in one UI.

The agent code is vendor-neutral OTLP/HTTP. To send the same data to a
different OTel backend, point `OPENCLAW_OTLP_ENDPOINT` somewhere else —
no code changes.

> **History:** this repo previously dual-exported to LangSmith /
> Phoenix via custom span enrichment. As of 2026-05-01 the project
> standardized on a single OTel backend and removed the custom
> enricher. As of 2026-05-04 the backend is the shiori LGTM stack
> as part of the Phase 2 fleet rebuild. Native LiveKit Agents 1.5+
> telemetry (`gen_ai.*` SemConv, `lk.*`) is sufficient on its own.
> The LangSmith provisioning tree (`ops/langsmith/`) is preserved as
> an archive — see `ops/langsmith/README.md` for how to reactivate it.

```
LiveKit agents (Python, this repo)
  ├─ OTel TracerProvider ──┬─ NoiseSpanFilter (drops agent_speaking / on_enter / on_exit / ...)
  │                        └─ BatchSpanProcessor → OTLP/HTTP → shiori:4318 → Tempo
  └─ OTel LoggerProvider ──── BatchLogRecordProcessor → OTLP/HTTP → shiori:4318 → Loki
                                  service.name=openclaw-livekit-{nyla,aoi,party}

OpenClaw Gateway (Node.js, ~/.openclaw)
  └─ diagnostics-otel plugin ─ traces + metrics + logs ─ OTLP/HTTP → shiori:4318 → Tempo/Loki/Mimir
                                  service.name=openclaw-gateway / openclaw-nyla
```

Both sources land in the **same** LGTM instance, so a Discord/SMS/voice
turn that crosses the gateway → LiveKit boundary shows up as one set
of correlatable services in the Grafana service map. The W3C
`traceparent` header propagation in OpenClaw means the gateway's
model-call span and the LiveKit agent's `llm_request` span can even
share a trace_id when the call originates upstream.

## TL;DR — wire your agents

The shiori LGTM stack is canonically managed outside this repo (its
compose lives at `~/Vaults/Aoi/wiki/services/observability/`).
Assuming it's already running on shiori, all this repo needs to do is
point its agents at it.

In `secrets/livekit-agents.env` (already done by default in
`config/secrets.env.example`):

```bash
OPENCLAW_OTEL_ENABLED=true
OPENCLAW_OTLP_ENDPOINT=http://shiori.mey.house:4318/v1/traces
OPENCLAW_OTEL_LOGS_ENABLED=true
OPENCLAW_DEPLOYMENT_ENVIRONMENT=harem-world
OPENCLAW_SERVICE_VERSION=dev
```

…then `make deploy`. Every span, log, and HTTP call your agents emit
now lands in shiori with the same `trace_id` linking traces to logs.

## Wiring the OpenClaw gateway

The OpenClaw gateway is wired to the same shiori LGTM stack as part
of fleet bring-up — see `~/Vaults/Aoi/wiki/services/observability.md`
and `~/Vaults/Aoi/wiki/gotchas/openclaw-otel-bundled-gate.md` for the
canonical procedure (`diagnostics-otel` plugin install + the runtime
patch required for non-bundled installs of the plugin).

Once wired, four service entries appear in the Grafana service map:

| service.name              | Source                          |
| ------------------------- | ------------------------------- |
| `openclaw-gateway` / `openclaw-nyla` | `~/.openclaw` Node.js gateway |
| `openclaw-livekit-nyla`   | This repo, voice agent          |
| `openclaw-livekit-aoi`    | This repo, voice agent          |
| `openclaw-livekit-party`  | This repo, voice agent          |

Gateway-specific span / metric prefixes you'll see (full reference in
[OpenClaw OTel docs](https://docs.openclaw.ai/gateway/opentelemetry)):

* **Spans**: `openclaw.model.usage`, `openclaw.model.call`,
  `openclaw.run`, `openclaw.harness.run`, `openclaw.tool.execution`,
  `openclaw.exec`, `openclaw.webhook.processed`,
  `openclaw.message.processed`, `openclaw.context.assembled`,
  `openclaw.tool.loop`.
* **Metrics**: `openclaw.tokens`, `openclaw.cost.usd`,
  `gen_ai.client.token.usage`, `gen_ai.client.operation.duration`,
  `openclaw.message.duration_ms`, `openclaw.queue.depth`,
  `openclaw.session.state`, `openclaw.memory.heap_used_bytes`,
  `openclaw.tool.loop.duration_ms`, plus a Node.js liveness family
  (`event_loop_delay_*`, `cpu_core_ratio`, `memory.rss_bytes`).

## Wiring the host (macOS) — host metrics, container stats, vendor uptime

The two sources above (LiveKit agents and OpenClaw gateway) cover the
*application* layer. To get **host metrics**, **container metrics**,
and **external vendor uptime** in the same shiori instance, run a
host-side OTel Collector via launchd. The collector is `otelcol-contrib`
v0.151.0 from the upstream
[opentelemetry-collector-releases](https://github.com/open-telemetry/opentelemetry-collector-releases)
project; install + plist + config templates all live in this repo.

```bash
make host-collector-install      # download binary + render configs + bootstrap launchd
make host-collector-status       # binary version, plist path, launchd state, recent logs
make host-collector-logs         # tail otel-collector.log + .err.log
make host-collector-restart      # re-render configs and bootstrap (picks up template changes)
make host-collector-uninstall    # remove launchd plist (binary + config preserved)
```

The collector runs **five pipelines**, each appearing as its own
`service.name` in Grafana:

| service.name                        | Pipeline               | What it surfaces                                                     |
| ----------------------------------- | ---------------------- | -------------------------------------------------------------------- |
| `host-mac`                          | `metrics/host`         | `system.cpu.*`, `system.memory.*`, `system.disk.*`, `system.network.*`, `system.filesystem.*`, `system.cpu.load_average.{1m,5m,15m}`, `system.processes.*` |
| Each container name (4+ services)   | `metrics/docker`       | `container.cpu.*`, `container.memory.*`, `container.network.*`, `container.blockio.*` (per container; the receiver mounts `/var/run/docker.sock`) |
| `httpcheck`                         | `metrics/httpcheck`    | `httpcheck.status` (per `http.url` × `http.status_code` × `http.status_class`), `httpcheck.duration`, `httpcheck.error` |
| `openclaw-gateway` (file-side)      | `logs/openclaw`        | Tails `~/.openclaw/logs/gateway.log` + `gateway.err.log` (additive to the OTLP-pushed logs from the gateway plugin)            |
| `openclaw-livekit-host` (file-side) | `logs/agents`          | Tails `<repo>/logs/voice/agent-*.log` + `*.err.log` (belt-and-suspenders for crashes before the agent OTel SDK initializes)    |

Container metrics: the host collector uses a transform processor that
copies `container.name` → `service.name`, so each container in your
local stack appears as its own service node in the Grafana service map
(`openclaw-redis`, `openclaw-livekit-server`, `openclaw-livekit-sip`,
`openclaw-livekit-egress`).

Vendor uptime: the `httpcheck` receiver pings these endpoints every 60s
unauthenticated. The metric is "vendor responded with HTTP" — auth
failures (`401`/`403`) are *good* signal because they mean the vendor
is reachable. To add or remove targets, edit
`config/otel-collector/config.yaml.template` and run
`make host-collector-restart`.

The collector exposes two debug surfaces locally:

* `http://127.0.0.1:13133/` — health check (200 = collector is healthy)
* `http://127.0.0.1:55679/debug/tracez` — zPages: live trace inspection
  *for the collector itself* (useful when a receiver is mute and you
  want to see whether spans are being dropped)

## The drill-down workflow

Everything in one place. Grafana walks the call narrative *and* gives
you the microscope.

1. **Open Grafana**: `http://shiori.mey.house:3000`.
2. **Find the call**: Explore → Tempo datasource. Filter by
   `service.name=openclaw-livekit-<agent>`, sort by duration, pick
   the one you care about. Or search by `session.id=<sip-call-id>`
   (set on the root `agent_session` span) or `enduser.id=<caller-phone>`
   if you have either from agent logs.
3. **Inspect the parent span**: the `agent_session` span carries
   `session.id` (SIP Call-ID), `enduser.id` (caller E.164),
   `openclaw.dialed_number`, `openclaw.caller_source`,
   `openclaw.lk_job_id`, plus a tree of child spans for each
   `agent_turn`, `function_tool`, `llm_request`, `tts_node`,
   and outbound `http.client` call.
4. **Drill into a slow tool**: click `function_tool: get_household_status`
   → see every `http.client` child span with `http.method`, `http.url`,
   `http.status_code`, exact timings, retries, and the gap between
   calls.
5. **Cross to logs in the same view**: Grafana's split view supports
   "Logs for this trace" — pivots the right pane to Loki filtered by
   `trace_id`. Every log line your agent emitted while that span was
   open shows up — connection warnings, retry-with-backoff messages,
   the actual upstream error.
6. **(Optional) Metrics**: the LiveKit dashboard shows aggregate
   latency per agent service, error rate over time, exception counts,
   so you can tell whether a slow run was an outlier or a trend.

## What's stamped on every span

### Resource attributes (every span and log)

| Key                       | Value                                                       |
| ------------------------- | ----------------------------------------------------------- |
| `service.name`            | `openclaw-livekit-<agent>` (e.g. `openclaw-livekit-aoi`)    |
| `service.namespace`       | `openclaw`                                                  |
| `service.version`         | `$OPENCLAW_SERVICE_VERSION` (default `dev`)                 |
| `service.instance.id`     | unique per process                                          |
| `deployment.environment`  | `$OPENCLAW_DEPLOYMENT_ENVIRONMENT` (default `harem-world`)  |
| `host.name`               | hostname                                                    |
| `process.pid`             | OS pid                                                      |

Grafana's Tempo service-graph groups by `service.name`; the environment
selector groups by `deployment.environment`.

### Span attributes (per-event)

LiveKit Agents 1.5+ emits these natively. We add zero enrichment.

GenAI semantic-convention keys (on `llm_request` / `agent_turn`):

* `gen_ai.system`, `gen_ai.request.model`, `gen_ai.operation.name`
* `gen_ai.usage.input_tokens`, `gen_ai.usage.output_tokens`,
  `gen_ai.usage.total_tokens`
* `gen_ai.server.time_to_first_token` (TTFT)
* `gen_ai.choice` events with the assistant's response text

LiveKit-specific keys (`lk.*`):

* `lk.function_tool.name`, `lk.function_tool.arguments`,
  `lk.function_tool.output` — every tool call
* `lk.transcript.text`, `lk.transcript.role` — STT / assistant turn text
* `lk.tts.duration`, `lk.tts.audio_duration` — TTS timings

Per-call routing identity (set by `attach_current_span_metadata`,
SemConv where one exists):

* `session.id` — SIP Call-ID (OTel SemConv standard for session keys)
* `enduser.id` — caller phone in E.164 (OTel SemConv); accepts PII
* `openclaw.dialed_number` — which DID the caller dialed
* `openclaw.caller_source` — `twilio` / `sip` / `livekit-cloud` / ...
* `openclaw.lk_job_id` — LiveKit job ID for cross-log correlation

### Errors

When a tool raises or the session aborts, the active span gets:

* `Status.ERROR`
* `span.record_exception(err)` — adds the stack trace as a span event

Tempo's span-list view filters on `status.code=error`; pair with the
LiveKit dashboard's error-rate panel for aggregate views.

## Auto-instrumented HTTP

Three instrumentors are wired automatically when
`OPENCLAW_OTEL_HTTP_INSTRUMENTATION` is unset (the default):

* `opentelemetry-instrumentation-httpx` — covers the openai plugin
  and google.genai SDK.
* `opentelemetry-instrumentation-aiohttp-client` — covers the
  elevenlabs plugin.
* `opentelemetry-instrumentation-requests` — covers the Twilio SDK
  and gateway HTTP calls.

Every outbound HTTP call your agent makes (Musubi v2, gateway, weather,
LLM/TTS provider) becomes a child `http.client` span with `http.method`,
`http.url`, `http.status_code`, and timings, plus an
`http.client.duration` histogram entry. That's how the "why was tool X
slow" workflow works and how the LiveKit Grafana dashboard's "HTTP
Request Duration" panel populates.

## Audio recording

Set `OPENCLAW_RECORD_AUDIO=true` and (optionally)
`OPENCLAW_AUDIO_PUBLIC_BASE_URL=https://media.example/recordings` to
have:

* LiveKit Egress write each call's audio to
  `${LIVEKIT_EGRESS_HOST_RECORDINGS_DIR}/<agent>/<call_sid>.ogg`.
* The agent stamps the active call span with `openclaw.audio.path`
  and, when configured, `openclaw.audio.url` so the recording is one
  click away in Grafana.

If you set the public base URL, Grafana shows it as a clickable link.
Without one, you'll see the local filesystem path and can `open` it
from a terminal.

## Managing the LGTM stack

The shiori LGTM stack itself is **not** managed from this repo.
Canonical compose + collector config + dashboards live at:

```
~/Vaults/Aoi/wiki/services/observability/
├── compose.yml              — Loki, Tempo, Mimir, Grafana, OTel Collector
├── config/
│   ├── loki/loki-config.yml
│   ├── tempo/tempo-config.yml
│   ├── mimir/mimir-config.yml
│   ├── grafana/{datasources,dashboards}.yml
│   └── otel-collector/otel-collector-config.yml
└── dashboards/              — Grafana dashboard JSONs (auto-provisioned)
```

The compose runs as `cd /opt/observability && docker compose up -d`
on shiori. To restart a single component:

```bash
ssh shiori.mey.house "cd /opt/observability && docker compose restart <service>"
```

Resource footprint on shiori: ~1.5 GB RAM at idle for the full LGTM
stack. Storage scales with retention (default Mimir retention: 14d;
Loki: 7d; Tempo: 24h). Tune in the per-component config files.

## Switching backends

Nothing in the agent code is shiori-specific — it's plain OTLP/HTTP.
To send the same data somewhere else:

1. Stand up a collector (or use the vendor's OTLP endpoint directly).
2. Point `OPENCLAW_OTLP_ENDPOINT` at it. Pass any auth headers via
   `OPENCLAW_OTLP_HEADERS` (`key=value,key2=value2`).
3. `make deploy`.

The same `gen_ai.*`, `lk.*`, `http.*`, and `service.*` attributes ship
to whichever target is configured. The `NoiseSpanFilter` is the only
custom span processor in the pipeline, and it just drops a handful of
LiveKit framework-internal spans (`agent_speaking`, `on_enter`, etc.)
to keep the trace tree readable. Set `OPENCLAW_OTEL_VERBOSE=true` to
disable that filter for deep dives.

## Verifying ingestion

Quick smoke test:

```bash
# 1. agents are up + sending
make health

# 2. agent env carries the OTel config
launchctl print "gui/$(id -u)/ai.openclaw.livekit-agent-nyla" \
  | grep OPENCLAW_OTEL

# 3. Mimir has metrics for our service
curl -sS 'http://shiori.mey.house:9009/prometheus/api/v1/label/service_name/values' | jq

# 4. Loki has logs for our service (last 5 min)
curl -sS -G 'http://shiori.mey.house:3100/loki/api/v1/query_range' \
  --data-urlencode 'query={service_name="openclaw-livekit-nyla"}' \
  --data-urlencode 'limit=5' \
  --data-urlencode "start=$(($(date +%s) - 300))000000000" | jq

# 5. Tempo has traces (via Grafana Explore)
#    Open http://shiori.mey.house:3000 → Explore → Tempo
#    Service: openclaw-livekit-nyla
```

If the Mimir query returns `openclaw-livekit-nyla` in `service_name`,
the metrics pipeline is healthy. If Loki returns recent stream entries,
the logs pipeline is healthy. If Tempo Explore shows recent traces,
the traces pipeline is healthy. The three pipelines are independent —
verifying each gives a complete picture.
