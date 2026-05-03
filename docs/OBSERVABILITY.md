# Observability — SigNoz (primary)

This repo emits **OpenTelemetry-first** spans, logs, and metrics and
ships them to a single primary backend: a self-hosted **SigNoz** stack
running locally. SigNoz is the call-narrative view, the slow-tool
microscope, the log explorer, the metrics dashboard, and the service
map — all in one UI on your laptop.

The agent code is vendor-neutral OTLP/HTTP. To send the same data to a
different OTel backend, point `OPENCLAW_OTLP_ENDPOINT` somewhere else —
no code changes.

> **History:** the repo previously dual-exported to LangSmith / Phoenix
> via custom span enrichment. As of 2026-05-01 we standardized on
> SigNoz alone and removed the custom enricher. Native LiveKit Agents
> 1.5+ telemetry (`gen_ai.*` SemConv, `lk.*`) is sufficient on its own.
> The LangSmith provisioning tree (`ops/langsmith/`) is preserved as an
> archive — see `ops/langsmith/README.md` for how to reactivate it.

```
LiveKit agents (Python, this repo)
  ├─ OTel TracerProvider ──┬─ NoiseSpanFilter (drops agent_speaking / on_enter / on_exit / ...)
  │                        └─ BatchSpanProcessor → OTLP/HTTP → SigNoz :4318
  └─ OTel LoggerProvider ──── BatchLogRecordProcessor → OTLP/HTTP → SigNoz :4318
                                  service.name=openclaw-livekit-{nyla,aoi,party}

OpenClaw Gateway (Node.js, ~/.openclaw)
  └─ diagnostics-otel plugin ─ traces + metrics + logs ─ OTLP/HTTP → SigNoz :4318
                                  service.name=openclaw-gateway
```

Both sources land in the **same** SigNoz instance, so a Discord/SMS/voice
turn that crosses the gateway → LiveKit boundary shows up as one set of
correlatable services in the SigNoz service map. The W3C `traceparent`
header propagation in OpenClaw means the gateway's model-call span and
the LiveKit agent's `llm_request` span can even share a trace_id when
the call originates upstream.

## TL;DR — boot the local stack

```bash
make signoz-up        # bootstraps ~/.signoz/signoz on first run, then docker compose up -d
make signoz           # opens http://localhost:8080 in your browser
```

> **First-run only**: SigNoz won't accept traces until you complete its
> in-UI onboarding (create the admin user + organization). On a fresh
> stack the otel-collector receives a no-op config and resets every
> OTLP connection until that signup is done — `signoz` container logs
> will repeatedly print `cannot create agent without orgId` until then.
> It's a one-time, local-only step (the credentials never leave your
> laptop).
>
> 1. Open `http://localhost:8080`.
> 2. Create the admin user + org through the prompted flow.
> 3. *Then* enable the OTLP exporter on your agents (next block).

In `secrets/livekit-agents.env` (already done by default for new clones):

```bash
OPENCLAW_OTEL_ENABLED=true
OPENCLAW_OTLP_ENDPOINT=http://localhost:4318/v1/traces
OPENCLAW_OTEL_LOGS_ENABLED=true
OPENCLAW_DEPLOYMENT_ENVIRONMENT=production
OPENCLAW_SERVICE_VERSION=signoz-primary
```

…then `make deploy`. Every span, log, and HTTP call your agents emit
now lands in SigNoz with the same `trace_id` linking traces to logs.

## Wiring the OpenClaw gateway

The same SigNoz stack also receives traces, metrics, and logs from the
locally-running OpenClaw gateway (`~/.openclaw/openclaw.json` +
`launchd:ai.openclaw.gateway`). The gateway ships its own
`diagnostics-otel` plugin — we just turn it on and point it at our
collector.

```bash
make signoz-wire-gateway        # apply config + restart the gateway
make signoz-verify-gateway      # read-only check of current state
```

What this does (idempotent — re-running is safe):

1. `openclaw config set diagnostics.otel.*` — endpoint
   `http://localhost:4318`, service name `openclaw-gateway`,
   `protocol=http/protobuf`, traces+metrics+logs all on, `sampleRate=1.0`,
   `flushIntervalMs=60000`.
2. `openclaw config set plugins.allow [...]` — appends `diagnostics-otel`
   to the gateway's plugin allowlist (idempotent merge — preserves your
   existing list).
3. `openclaw plugins enable diagnostics-otel` — flips the enabled bit.
4. `launchctl kickstart -k gui/<uid>/ai.openclaw.gateway` — restart the
   gateway so the new plugin loads. Falls back to `openclaw gateway
   restart` if the service is not under launchd.

`captureContent.*` defaults to **all five subkeys on** (`inputMessages`,
`outputMessages`, `toolInputs`, `toolOutputs`, `systemPrompt`) so the
gateway's debugging visibility matches what the LiveKit agents already
export. To dial it back, set `OPENCLAW_GW_CAPTURE` before running:

```bash
OPENCLAW_GW_CAPTURE=tools_only make signoz-wire-gateway   # tool I/O only
OPENCLAW_GW_CAPTURE=none       make signoz-wire-gateway   # bounded ids only
```

After it's wired, you'll see four service entries in SigNoz' service
map:

| service.name              | Source                          |
| ------------------------- | ------------------------------- |
| `openclaw-gateway`        | `~/.openclaw` Node.js gateway   |
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
and **external vendor uptime** in the same SigNoz instance, run a
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
`service.name` in SigNoz:

| service.name                        | Pipeline               | What it surfaces                                                     |
| ----------------------------------- | ---------------------- | -------------------------------------------------------------------- |
| `host-mac`                          | `metrics/host`         | `system.cpu.*`, `system.memory.*`, `system.disk.*`, `system.network.*`, `system.filesystem.*`, `system.cpu.load_average.{1m,5m,15m}`, `system.processes.*` |
| Each container name (8+ services)   | `metrics/docker`       | `container.cpu.*`, `container.memory.*`, `container.network.*`, `container.blockio.*` (per container; the receiver mounts `/var/run/docker.sock`) |
| `httpcheck`                         | `metrics/httpcheck`    | `httpcheck.status` (per `http.url` × `http.status_code` × `http.status_class`), `httpcheck.duration`, `httpcheck.error` |
| `openclaw-gateway` (file-side)      | `logs/openclaw`        | Tails `~/.openclaw/logs/gateway.log` + `gateway.err.log` (additive to the OTLP-pushed logs from the gateway plugin)            |
| `openclaw-livekit-host` (file-side) | `logs/agents`          | Tails `<repo>/logs/voice/agent-*.log` + `*.err.log` (belt-and-suspenders for crashes before the agent OTel SDK initializes)    |

Container metrics: the host collector uses a transform processor that
copies `container.name` → `service.name`, so the eight containers in
your local stack each appear as their own service node in the SigNoz
service map (`openclaw-redis`, `openclaw-livekit-server`,
`openclaw-livekit-sip`, `openclaw-livekit-egress`, `signoz`,
`signoz-clickhouse`, `signoz-zookeeper-1`, `signoz-otel-collector`).

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

Everything in one place. The same UI walks the call narrative *and*
gives you the microscope.

1. **Open Traces in SigNoz**: `http://localhost:8080` → Traces.
2. **Find the call**: filter by `service.name=openclaw-livekit-<agent>`,
   sort by duration, pick the one you care about. Or search by
   `session.id=<sip-call-id>` (set on the root `agent_session` span)
   or `enduser.id=<caller-phone>` if you have either from agent logs.
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
5. **Cross to logs in the same view**: SigNoz's Logs tab is filterable
   by `trace_id` (auto-injected by `LoggingInstrumentor`). Every log
   line your agent emitted while that span was open shows up — connection
   warnings, retry-with-backoff messages, the actual upstream error.
6. **(Optional) Metrics**: Service Map shows aggregate latency per
   agent service, error rate over time, exception counts, so you can
   tell whether a slow run was an outlier or a trend.

## What's stamped on every span

### Resource attributes (every span and log)

| Key                       | Value                                                       |
| ------------------------- | ----------------------------------------------------------- |
| `service.name`            | `openclaw-livekit-<agent>` (e.g. `openclaw-livekit-aoi`)    |
| `service.namespace`       | `openclaw`                                                  |
| `service.version`         | `$OPENCLAW_SERVICE_VERSION` (default `dev`)                 |
| `service.instance.id`     | unique per process                                          |
| `deployment.environment`  | `$OPENCLAW_DEPLOYMENT_ENVIRONMENT` (default `production`)   |
| `host.name`               | hostname                                                    |
| `process.pid`             | OS pid                                                      |

SigNoz's Service Map groups by `service.name`; the environment selector
groups by `deployment.environment`.

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

SigNoz's Exceptions tab aggregates these by exception type per service.

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
`http.client.duration` histogram entry. That's how the SigNoz
"why was tool X slow" workflow works and how the LiveKit dashboard's
"HTTP Request Duration" panel populates.

## Audio recording

Set `OPENCLAW_RECORD_AUDIO=true` and (optionally)
`OPENCLAW_AUDIO_PUBLIC_BASE_URL=https://media.example/recordings` to
have:

* LiveKit Egress write each call's audio to
  `${LIVEKIT_EGRESS_HOST_RECORDINGS_DIR}/<agent>/<call_sid>.ogg`.
* The agent stamps the active call span with `openclaw.audio.path`
  and, when configured, `openclaw.audio.url` so the recording is one
  click away in SigNoz.

If you set the public base URL, SigNoz shows it as a clickable link.
Without one, you'll see the local filesystem path and can `open` it
from a terminal.

## Managing the SigNoz stack

| Command                              | What it does                                                       |
| ------------------------------------ | ------------------------------------------------------------------ |
| `make signoz-up`                     | Clone (first run only) + `docker compose up -d`                    |
| `make signoz`                        | Open `http://localhost:8080`                                       |
| `make signoz-status`                 | `docker compose ps`                                                |
| `make signoz-logs ARGS=<service>`    | Follow logs (`signoz`, `clickhouse`, `otel-collector`, ...)        |
| `make signoz-down`                   | Stop containers, **preserve data**                                 |
| `make signoz-update`                 | `git pull` upstream SigNoz; rerun `signoz-up` to apply             |
| `make signoz-nuke`                   | `docker compose down -v` — **deletes all data**                    |

The clone target is `${OPENCLAW_SIGNOZ_HOME:-~/.signoz/signoz}`. SigNoz
runs on its own Docker network (`signoz-net`), so it doesn't conflict
with the LiveKit compose stack.

### Resource footprint

SigNoz's full stack uses ~6 containers: ClickHouse, Zookeeper,
query-service+UI, OTel collector, schema-migrator. Plan for ~2 GB RAM
at idle and a few GB of disk for ClickHouse over time.

If your dev machine is tight, run `make signoz-down` between sessions
— state survives, RAM is freed.

### Pinning a version

The clone tracks `main` by default. To pin a release:

```bash
OPENCLAW_SIGNOZ_REF=v0.69.0 make signoz-up
```

Or set `OPENCLAW_SIGNOZ_REF` in your shell rc.

## Importing dashboards into SigNoz

The repo ships **three** dashboard JSONs in `ops/signoz/dashboards/`,
each copied verbatim from the
[upstream SigNoz dashboards repo](https://github.com/SigNoz/dashboards).
We deliberately do not ship custom dashboards; native templates work
because LiveKit emits the attributes they expect.

| Dashboard                          | Audience      | Drill purpose                                                                  |
| ---------------------------------- | ------------- | ------------------------------------------------------------------------------ |
| `livekit-dashboard.json`           | Voice agents  | Tokens, models, error rate, P95 latency, TTS duration, conversation analytics. |
| `hostmetrics-dashboard.json`       | Host (macOS)  | CPU / memory / disk / network / load average / processes.                      |
| `container-metrics-dashboard.json` | Docker        | Per-container CPU / memory / network / blockio for the SigNoz + LiveKit stacks.|

To import all three at once (one-time, after SigNoz first-run UI signup):

```bash
SIGNOZ_USER=you@example.com \
SIGNOZ_PASS='...' \
make signoz-import-dashboards
```

The script POSTs every `*.json` in `ops/signoz/dashboards/` to
`http://localhost:8080/api/v1/dashboards`. SigNoz dedupes by title,
so a second run is a safe no-op.

The LiveKit dashboard maps cleanly onto the spans the framework emits —
every panel populates without code changes:

| Panel                                | Trace filter                                              |
| ------------------------------------ | --------------------------------------------------------- |
| Input / Output Tokens, Token Usage   | `name=llm_request` + `gen_ai.usage.*`                     |
| Model Distribution                   | `gen_ai.request.model` (groupBy)                          |
| Error Rate, Errors                   | `has_error=true`                                          |
| Agent Response Latency (P95)         | `name=agent_turn` duration                                |
| Number of Conversations / Avg Turns  | `name=agent_turn` count per `trace_id`                    |
| TTS Duration                         | `name=tts_node` duration                                  |
| Services and Languages               | `name=job_entrypoint`, groupBy `service.name`, lang       |
| HTTP Request Duration                | `http.client.duration` histogram (OTel HTTP instrumentor) |
| Logs                                 | `service.name` (OTel logs)                                |

Or paste any JSON manually via the SigNoz UI:
**Dashboards → + New dashboard → Import JSON** → pick a file from
`ops/signoz/dashboards/`.

To pull updates from upstream:

```bash
curl -fsSL "https://raw.githubusercontent.com/SigNoz/dashboards/main/livekit/livekit-dashboard.json" \
  -o ops/signoz/dashboards/livekit-dashboard.json
make signoz-import-dashboards
```

Roll your own panels by querying ClickHouse directly via the SigNoz
Query Builder; the relevant tables are `signoz_traces.signoz_index_v3`
and `signoz_logs.logs_v2`.

## Switching backends

Nothing in the agent code is SigNoz-specific — it's plain OTLP/HTTP.
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

# 2. agent env carries the SigNoz config
launchctl print "gui/$(id -u)/ai.openclaw.livekit-agent-nyla" \
  | grep OPENCLAW_OTEL

# 3. SigNoz collector accepted traces
docker logs signoz-otel-collector 2>&1 | tail -30

# 4. ClickHouse has spans for our service
docker exec signoz-clickhouse clickhouse-client --query \
  "SELECT serviceName, name, count() FROM signoz_traces.signoz_index_v3 \
   WHERE timestamp > now() - INTERVAL 5 MINUTE \
   GROUP BY serviceName, name ORDER BY count() DESC"
```

If the third query returns rows for `openclaw-livekit-nyla` /
`openclaw-livekit-aoi` / `openclaw-livekit-party`, you're done.
