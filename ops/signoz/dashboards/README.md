# SigNoz dashboards (committed JSON)

JSON dashboard definitions imported into the local SigNoz instance.
Source of truth: this folder. Edit the JSON, re-run
`make signoz-import-dashboards`, commit.

## Files

| File                     | Source                                                                                  | Notes                                                                                              |
| ------------------------ | --------------------------------------------------------------------------------------- | -------------------------------------------------------------------------------------------------- |
| `livekit-dashboard.json` | [SigNoz/dashboards](https://github.com/SigNoz/dashboards/tree/main/livekit) (upstream)  | Token usage, model distribution, error rate, P95 latency, TTS duration, conversation analytics.   |

## Importing

```bash
SIGNOZ_USER=you@example.com SIGNOZ_PASS=... make signoz-import-dashboards
```

The script POSTs every `*.json` in this folder to
`http://localhost:8080/api/v1/dashboards`. It uses the admin credentials
you set up during the SigNoz first-run UI signup. SigNoz dedupes by
title, so a second run is a safe no-op (returns HTTP 409 per file).

You can also import manually: in the SigNoz UI go to **Dashboards →
+ New dashboard → Import JSON** and paste the file contents.

## What lights up the LiveKit dashboard

The dashboard filters traces by these stock LiveKit span names:

* `name=llm_request` — fed by `LLMStream._llm_request_span_name`
  (LiveKit emits one per LLM call).
* `name=agent_turn` — emitted by `agent_activity._traceable_run_turn`.
* `name=tts_request` — fed by `TTSStream._tts_request_span_name`.
* `name=job_entrypoint` — emitted in `job_proc_lazy_main.py`.

Plus these GenAI semantic-convention attributes (also stock LiveKit):

* `gen_ai.request.model`
* `gen_ai.usage.input_tokens` / `gen_ai.usage.output_tokens`
* `gen_ai.operation.name=chat`
* `gen_ai.provider.name`

The "HTTP Request Duration" panel reads the `http.client.duration`
histogram, which the OTel SDK auto-emits once a `MeterProvider` is set
and `aiohttp` / `requests` are instrumented. The OpenClaw SDK enables
this whenever `OPENCLAW_OTEL_ENABLED=true` and the OTLP exporter is
configured (the default).

## Refreshing from upstream

To pull the latest dashboard JSON from upstream:

```bash
curl -fsSL "https://raw.githubusercontent.com/SigNoz/dashboards/main/livekit/livekit-dashboard.json" \
  -o ops/signoz/dashboards/livekit-dashboard.json
make signoz-import-dashboards
```
