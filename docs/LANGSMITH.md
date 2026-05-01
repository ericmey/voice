# LangSmith (archived)

> **As of 2026-05-01 the repo standardized on SigNoz as the primary
> observability backend.** This document is archived. For the active
> observability docs see **[OBSERVABILITY.md](OBSERVABILITY.md)**.

The LangSmith provisioning code (`ops/langsmith/`) is kept in-tree as
an opt-in component but is not part of the default agent runtime. The
former `LangSmithSpanProcessor` was renamed to `LiveKitOtelEnricher`
(in `sdk/src/sdk/livekit_otel_enricher.py`) at the same time and is now
vendor-neutral — it writes the same `gen_ai.*` semantic-convention
attributes to whichever backend is configured.

## Why we moved off LangSmith as the primary

* **Single pane of glass.** SigNoz hosts traces, logs, metrics, the
  service map, and exception tracking under one URL. LangSmith is
  call-narrative only; everything else (HTTP child spans, log
  correlation, P95 latency by service) was already happening in SigNoz.
* **Open telemetry, open data.** ClickHouse on disk, queryable however
  we want. No per-trace pricing, no PII leaving the laptop.
* **One set of conventions.** SigNoz reads stock GenAI semantic-convention
  attributes (`gen_ai.*`, `openinference.*`, `http.*`, `service.*`) — the
  same attributes the agent SDK already emits.

## How to reactivate LangSmith if you ever need it

1. Add `langsmith` to `OPENCLAW_OTEL_EXPORTERS` in
   `secrets/livekit-agents.env`:

   ```bash
   OPENCLAW_OTEL_EXPORTERS=otlp,langsmith
   OTEL_EXPORTER_OTLP_ENDPOINT=https://api.smith.langchain.com/otel
   OTEL_EXPORTER_OTLP_HEADERS="x-api-key=...,Langsmith-Project=Harem World"
   LANGSMITH_TRACING=true
   ```

2. (Optional) Re-run the LangSmith infrastructure-as-code:

   ```bash
   make langsmith-plan-legacy
   make langsmith-provision-legacy
   ```

3. `make deploy`.

The `LiveKitOtelEnricher` will continue to write
`langsmith.span.kind` / `langsmith.metadata.*` mirrors alongside the
canonical `gen_ai.*` attributes, and the second exporter will fan the
same data out to LangSmith. The SigNoz path keeps working unchanged.
