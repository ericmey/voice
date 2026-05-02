# LangSmith (archived)

> **As of 2026-05-01 the repo standardized on SigNoz as the primary
> (and only) observability backend.** This document is archived. For
> the active observability docs see
> **[OBSERVABILITY.md](OBSERVABILITY.md)**.

## What was removed

* The custom OTel span enricher
  (`sdk/src/sdk/livekit_otel_enricher.py`, formerly the
  `LangSmithSpanProcessor`) — its mappings duplicated what LiveKit
  Agents 1.5+ already emits natively.
* The multi-exporter knob (`OPENCLAW_OTEL_EXPORTERS`) and the
  `_rewrite_langsmith_project_header` helper in `sdk/tracing.py`.
* The dual-export to `https://api.smith.langchain.com/otel`.

## Why we moved off LangSmith as the primary

* **Single pane of glass.** SigNoz hosts traces, logs, metrics, the
  service map, and exception tracking under one URL. LangSmith is
  call-narrative only; everything else (HTTP child spans, log
  correlation, P95 latency by service) was already in SigNoz.
* **Open telemetry, open data.** ClickHouse on disk, queryable however
  we want. No per-trace pricing, no PII leaving the laptop.
* **One set of conventions.** LiveKit Agents 1.5+ emits the GenAI
  semantic-convention attributes (`gen_ai.*`) SigNoz dashboards
  already read. Custom enrichment only added duplicates.

## What still exists

* `ops/langsmith/` — provisioning code (project setup, datasets,
  rules) preserved as an archive in case the LangSmith UX ever needs
  to be reactivated for evals or judges.
* `Makefile` targets `langsmith-plan-legacy` / `langsmith-provision-legacy`
  still call into `ops/langsmith/provision`.

## Reactivating LangSmith

There is no built-in dual-export pathway any more. If you need LangSmith
again, the cleanest options are:

1. Run a local OTel collector (`otelcol-contrib`) that fans out from
   one OTLP receiver to two OTLP exporters — one to SigNoz, one to
   `https://api.smith.langchain.com/otel`. Point `OPENCLAW_OTLP_ENDPOINT`
   at the local collector. No code changes here.
2. Or revert this commit's removal of `livekit_otel_enricher.py` and
   the `OPENCLAW_OTEL_EXPORTERS` branching in `sdk/tracing.py` — the
   git history has the full prior implementation.

The agent SDK itself stays vendor-neutral OTLP/HTTP either way.
