# SigNoz dashboards (committed JSON)

Three published SigNoz dashboard templates, copied verbatim from
[`SigNoz/dashboards`](https://github.com/SigNoz/dashboards) and tracked
here so re-imports are reproducible. We deliberately do **not** ship
custom dashboards — every panel below comes from a template the SigNoz
team owns and updates. Native-first.

## Files

| File                               | Upstream source                                                                                                                            | What it shows                                                                                                                                          |
| ---------------------------------- | ------------------------------------------------------------------------------------------------------------------------------------------ | ------------------------------------------------------------------------------------------------------------------------------------------------------ |
| `livekit-dashboard.json`           | [`livekit/livekit-dashboard.json`](https://github.com/SigNoz/dashboards/blob/main/livekit/livekit-dashboard.json)                          | Token usage, model distribution, error rate, P95 latency, TTS duration, conversation analytics. Reads native LiveKit `gen_ai.*` + `lk.*` attributes. |
| `hostmetrics-dashboard.json`       | [`hostmetrics/hostmetrics.json`](https://github.com/SigNoz/dashboards/blob/main/hostmetrics/hostmetrics.json)                              | CPU / memory / disk / network / load on the host running the agents. Reads metrics from the local `otelcol-contrib` `hostmetrics` receiver.            |
| `container-metrics-dashboard.json` | [`container-metrics/docker/container-metrics-by-host.json`](https://github.com/SigNoz/dashboards/blob/main/container-metrics/docker/container-metrics-by-host.json) | Per-container CPU / memory / network / IO. Reads metrics from the `otelcol-contrib` `docker_stats` receiver (on by default in our config).           |

## Importing

```bash
SIGNOZ_USER=you@example.com SIGNOZ_PASS=... make signoz-import-dashboards
```

The script POSTs every `*.json` in this folder to
`http://localhost:8080/api/v1/dashboards`. Uses the admin credentials
from the SigNoz first-run UI signup. SigNoz dedupes by title, so a
second run is a safe no-op (HTTP 409 per file).

You can also import manually: in the SigNoz UI go to **Dashboards →
+ New dashboard → Import JSON** and paste the file contents.

## What the LiveKit dashboard reads

LiveKit Agents 1.5+ emits OTel spans with native `gen_ai.*` semantic-
convention attributes — the dashboard depends on these without any
SDK-side enrichment from us. The relevant native span names:

* `name=llm_request` — emitted by `LLMStream._llm_request_span_name`.
* `name=agent_turn` — emitted by `agent_activity._traceable_run_turn`.
* `name=tts_node` — emitted by LiveKit's TTS node instrumentation.
* `name=job_entrypoint` — emitted in `job_proc_lazy_main.py`.

The native GenAI attributes the dashboard groups by:

* `gen_ai.request.model`
* `gen_ai.usage.input_tokens` / `gen_ai.usage.output_tokens`
* `gen_ai.operation.name=chat`
* `gen_ai.provider.name`

The "HTTP Request Duration" panel reads the `http.client.duration`
histogram, auto-emitted by the OTel SDK once a `MeterProvider` is set
and `httpx` / `aiohttp` / `requests` are instrumented. The OpenClaw
SDK enables all three when `OPENCLAW_OTEL_ENABLED=true` and
`OPENCLAW_OTLP_ENDPOINT` is configured (the default).

## Refreshing from upstream

The three templates are pinned versions, not symlinks. To refresh:

```bash
curl -fsSL "https://raw.githubusercontent.com/SigNoz/dashboards/main/livekit/livekit-dashboard.json" \
  -o ops/signoz/dashboards/livekit-dashboard.json
curl -fsSL "https://raw.githubusercontent.com/SigNoz/dashboards/main/hostmetrics/hostmetrics.json" \
  -o ops/signoz/dashboards/hostmetrics-dashboard.json
curl -fsSL "https://raw.githubusercontent.com/SigNoz/dashboards/main/container-metrics/docker/container-metrics-by-host.json" \
  -o ops/signoz/dashboards/container-metrics-dashboard.json

make signoz-import-dashboards
```

If a panel breaks after a refresh, diff the JSON to see what upstream
changed; do **not** patch the file in place. The whole point of
copying templates verbatim is that we can roll forward by re-pulling.

## What lights up each dashboard

| Dashboard         | Data source                                                                  | Wired by                                                                                                          |
| ----------------- | ---------------------------------------------------------------------------- | ----------------------------------------------------------------------------------------------------------------- |
| LiveKit           | OTLP traces + `http.client.duration` histogram from each agent process       | `sdk/src/sdk/tracing.py` (called from every agent at startup via `setup_otel_tracing()`)                          |
| Host Metrics      | OTLP metrics from `otelcol-contrib`'s `hostmetrics` receiver                 | `make host-collector-install` (renders `config/otel-collector/config.yaml.template` and runs the collector under launchd) |
| Container Metrics | OTLP metrics from `otelcol-contrib`'s `docker_stats` receiver                | Same host collector — `docker_stats` is enabled in `config/otel-collector/config.yaml.template`                   |

If a dashboard is empty in SigNoz, the cause is almost always one of
the three sources above being off. Verify in this order:

```bash
make signoz-status            # SigNoz containers up?
make host-collector-status    # otelcol-contrib running?
make signoz-verify-gateway    # gateway plugin enabled? (LLM-side calls)
```
