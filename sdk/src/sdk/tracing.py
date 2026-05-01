"""OpenTelemetry tracing, logs, and metrics setup for OpenClaw LiveKit
agents.

The pipeline is OTel-first: every span uses standard semantic conventions
(``gen_ai.*``, ``openinference.*``, ``http.*``, ``service.*``) so it works
unchanged across SigNoz, Phoenix, Honeycomb, Datadog, Jaeger, LangSmith,
or any OTLP-compatible backend. SigNoz is the default primary backend
(see ``docs/OBSERVABILITY.md``).

Architecture::

    TracerProvider(Resource(service.name=..., service.version=..., agent=...))
      ├── LiveKitOtelEnricher                        (enrichment only)
      │     - lk.* → gen_ai.*, openinference.*, openclaw.*
      │     - thread_id grouping, deferred job spans
      ├── BatchSpanProcessor → OTLP/HTTP → SigNoz    (default)
      ├── BatchSpanProcessor → OTLP/HTTP → LangSmith (optional)
      ├── BatchSpanProcessor → OTLP/HTTP → Phoenix   (optional)
      └── BatchSpanProcessor → ConsoleSpanExporter   (when OPENCLAW_OTEL_DEBUG=true)

    LoggerProvider(...) → BatchLogRecordProcessor → OTLP/HTTP   (logs)
    MeterProvider(...)  → PeriodicExportingMetricReader → OTLP  (metrics)

Auto-instrumentation: ``aiohttp-client``, ``requests``, ``logging``, and
optional system-metrics are wired so every outbound HTTP call becomes a
child ``http.client`` span (with histogram metric), every Python log
record carries ``trace_id`` / ``span_id`` / ``service.name`` for
correlation, and host CPU/memory/disk gauges populate when the
``opentelemetry-instrumentation-system-metrics`` package is installed.

Configuration (see ``config/secrets.env.example`` for defaults):

* ``OPENCLAW_OTEL_ENABLED=true`` — master switch (legacy alias:
  ``LANGSMITH_TRACING=true``).
* ``OPENCLAW_OTEL_EXPORTERS`` — comma-separated list. Default ``otlp``.
  Recognized values: ``otlp``, ``phoenix``, ``langsmith``, ``console``.
* Per-exporter knobs:
  - ``OPENCLAW_OTLP_ENDPOINT`` / ``OPENCLAW_OTLP_HEADERS`` — SigNoz or
    any generic OTLP/HTTP target. Default
    ``http://localhost:4318/v1/traces``.
  - ``OPENCLAW_PHOENIX_ENDPOINT`` (default
    ``http://localhost:6006/v1/traces``).
  - ``OTEL_EXPORTER_OTLP_ENDPOINT`` / ``OTEL_EXPORTER_OTLP_HEADERS`` —
    LangSmith. Per-agent ``Langsmith-Project`` is rewritten in.
* ``OPENCLAW_OTEL_DEBUG=true`` — adds the console exporter for local diag.
* ``OPENCLAW_OTEL_HTTP_INSTRUMENTATION=false`` — disable HTTP auto-instr.
* ``OPENCLAW_OTEL_LOGS_ENABLED`` / ``OPENCLAW_OTEL_METRICS_ENABLED`` —
  explicit overrides (auto-on alongside an OTLP exporter).

Setup MUST happen before ``AgentServer()`` is instantiated; LiveKit caches
the tracer provider at server-construction time. The agent ``load_env()``
calls :func:`setup_otel_tracing` at module-top before any LiveKit import.
"""

from __future__ import annotations

import atexit
import logging
import os
import platform
import socket
import uuid
from typing import Any

logger = logging.getLogger("openclaw-livekit.tracing")

_initialized = False
_provider: Any | None = None
_logger_provider: Any | None = None
_meter_provider: Any | None = None
_atexit_registered = False
_instance_id = uuid.uuid4().hex


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def setup_otel_tracing() -> None:
    """Wire OTel tracing if enabled. Idempotent.

    Reads ``OPENCLAW_OTEL_ENABLED`` (or legacy ``LANGSMITH_TRACING``).
    Configures the TracerProvider with one enrichment processor plus one
    BatchSpanProcessor per configured exporter, then publishes the
    provider to both the global OTel registry and LiveKit's own dynamic
    tracer wrapper.
    """
    global _initialized, _provider, _atexit_registered

    pid = os.getpid()
    _debug(f"[OTEL-SETUP] pid={pid} entered setup_otel_tracing")

    if _initialized:
        _debug(f"[OTEL-SETUP] pid={pid} already initialized, skipping")
        return

    if not _otel_enabled():
        _debug(f"[OTEL-SETUP] pid={pid} OPENCLAW_OTEL_ENABLED off — disabled")
        return

    # Optional but cheap: rewrite the LangSmith project header from the
    # agent name so each agent lands in its own LangSmith project even
    # when only OTEL_EXPORTER_OTLP_HEADERS is set.
    _rewrite_langsmith_project_header()

    try:
        from livekit.agents.telemetry import set_tracer_provider as set_livekit_tracer_provider
        from opentelemetry import trace as otel_trace
        from opentelemetry.sdk.trace import TracerProvider

        from sdk.livekit_otel_enricher import LiveKitOtelEnricher
    except ImportError as exc:
        _debug(f"[OTEL-SETUP] pid={pid} ImportError: {exc} — disabled")
        logger.warning(
            "OPENCLAW_OTEL_ENABLED=true but OTel deps not installed (%s) — disabled. "
            "Run `uv sync` from the repository root to refresh the workspace environment.",
            exc,
        )
        return

    provider = TracerProvider(resource=_build_resource())

    # Enrichment processor: maps LiveKit lk.* attrs to OTel-GenAI
    # semantic-convention keys plus OpenClaw filter dimensions. Runs
    # once, BEFORE the export processors, so every backend sees the
    # same enriched span.
    provider.add_span_processor(LiveKitOtelEnricher(enrichment_only=True))

    exporters = _configure_exporters(provider)
    if not exporters:
        logger.warning(
            "OTEL tracing enabled but no exporters configured — set OPENCLAW_OTEL_EXPORTERS"
        )
        return

    otel_trace.set_tracer_provider(provider)
    set_livekit_tracer_provider(provider)

    if not _atexit_registered:
        atexit.register(shutdown_otel_tracing)
        _atexit_registered = True

    if _http_instrumentation_enabled():
        _install_http_instrumentation(provider)

    if _logs_enabled():
        _install_logs_pipeline(provider.resource)

    if _metrics_enabled():
        _install_metrics_pipeline(provider.resource)

    _provider = provider
    _initialized = True
    _debug(
        f"[OTEL-SETUP] pid={pid} ENABLED — exporters={exporters} "
        f"resource={dict(provider.resource.attributes)}"
    )
    logger.info("OTel tracing enabled (exporters=%s)", ",".join(exporters))


def force_flush_otel_tracing(timeout_millis: int = 10000) -> bool:
    """Flush pending spans + log records + metric points before exit."""
    ok = True
    if _provider is not None:
        try:
            ok = bool(_provider.force_flush(timeout_millis))
        except Exception as exc:
            logger.warning("OTel tracing force_flush failed: %s", exc)
            ok = False
    if _logger_provider is not None:
        try:
            ok = bool(_logger_provider.force_flush(timeout_millis)) and ok
        except Exception as exc:
            logger.warning("OTel logs force_flush failed: %s", exc)
            ok = False
    if _meter_provider is not None:
        try:
            ok = bool(_meter_provider.force_flush(timeout_millis)) and ok
        except Exception as exc:
            logger.warning("OTel metrics force_flush failed: %s", exc)
            ok = False
    return ok


def shutdown_otel_tracing() -> None:
    """Best-effort process-exit shutdown for traces, logs, and metrics."""
    if _provider is not None:
        try:
            _provider.shutdown()
        except Exception as exc:
            logger.warning("OTel tracing shutdown failed: %s", exc)
    if _logger_provider is not None:
        try:
            _logger_provider.shutdown()
        except Exception as exc:
            logger.warning("OTel logs shutdown failed: %s", exc)
    if _meter_provider is not None:
        try:
            _meter_provider.shutdown()
        except Exception as exc:
            logger.warning("OTel metrics shutdown failed: %s", exc)


def wire_otel_shutdown_flush(ctx: Any, timeout_millis: int = 10000) -> None:
    """Flush pending spans when LiveKit tears down a job."""
    add_shutdown_callback = getattr(ctx, "add_shutdown_callback", None)
    if add_shutdown_callback is None:
        return
    try:
        add_shutdown_callback(lambda: force_flush_otel_tracing(timeout_millis))
    except Exception as exc:
        logger.warning("OTel shutdown flush hook registration failed: %s", exc)


def attach_current_span_metadata(**metadata: Any) -> None:
    """Attach call/job metadata to the active OTel span.

    LiveKit's ``AgentSession.start`` leaves the session span in the active
    OTel context. Agent entrypoints call this after caller resolution so
    the root run carries call_sid, caller, room, route/source.

    Writes both ``langsmith.metadata.*`` (LangSmith sidebar / filters)
    and plain ``otel`` attribute keys (visible in any backend).
    """
    try:
        from opentelemetry import trace as otel_trace
    except ImportError:
        return

    span = otel_trace.get_current_span()
    if span is None or not span.is_recording():
        return

    exported_metadata: dict[str, str] = {}
    for key, value in metadata.items():
        if value is None or value == "":
            continue
        clean_key = key.replace(".", "_")
        value_str = str(value)
        # Plain OTel-visible attribute (any backend).
        span.set_attribute(f"openclaw.{clean_key}", value_str)
        # LangSmith sidebar / filter mirror.
        ls_key = f"langsmith.metadata.{clean_key}"
        span.set_attribute(ls_key, value_str)
        exported_metadata[ls_key] = value_str

    if not exported_metadata:
        return
    try:
        from .livekit_otel_enricher import remember_live_trace_call_metadata

        remember_live_trace_call_metadata(span.get_span_context().trace_id, exported_metadata)
    except Exception:
        return


# ---------------------------------------------------------------------------
# Internals
# ---------------------------------------------------------------------------


def _otel_enabled() -> bool:
    for var in ("OPENCLAW_OTEL_ENABLED", "LANGSMITH_TRACING"):
        value = os.environ.get(var, "").lower()
        if value in ("true", "1", "yes"):
            return True
    return False


def _http_instrumentation_enabled() -> bool:
    return os.environ.get("OPENCLAW_OTEL_HTTP_INSTRUMENTATION", "true").lower() not in (
        "false",
        "0",
        "no",
    )


def _debug_enabled() -> bool:
    for var in ("OPENCLAW_OTEL_DEBUG", "LANGSMITH_PROCESSOR_DEBUG"):
        if os.environ.get(var, "").lower() in ("true", "1", "yes"):
            return True
    return False


def _debug(message: str) -> None:
    if not _debug_enabled():
        return
    import sys as _sys

    print(message, file=_sys.stderr, flush=True)


def _agent_name() -> str:
    return (os.environ.get("OPENCLAW_AGENT_NAME") or "").strip().lower() or "unknown"


def _agent_langsmith_project() -> str | None:
    explicit = os.environ.get("LANGSMITH_PROJECT")
    if explicit:
        return explicit
    name = _agent_name()
    if name in ("", "unknown"):
        return None
    return {
        "aoi": "Aoi",
        "nyla": "Nyla",
        "party": "Party",
    }.get(name, name.title())


def _rewrite_langsmith_project_header() -> None:
    headers = os.environ.get("OTEL_EXPORTER_OTLP_HEADERS")
    project = _agent_langsmith_project()
    if not headers or not project:
        return
    parts = [part for part in headers.split(",") if part]
    parts = [part for part in parts if not part.lower().startswith("langsmith-project=")]
    parts.append(f"Langsmith-Project={project}")
    new_headers = ",".join(parts)
    os.environ["OTEL_EXPORTER_OTLP_HEADERS"] = new_headers
    _debug(f"[OTEL-SETUP] LangSmith project header set to {project!r}")


def _build_resource() -> Any:
    """Identify this process to every backend."""
    from opentelemetry.sdk.resources import (
        DEPLOYMENT_ENVIRONMENT,
        HOST_NAME,
        PROCESS_PID,
        SERVICE_INSTANCE_ID,
        SERVICE_NAME,
        SERVICE_NAMESPACE,
        SERVICE_VERSION,
        Resource,
    )

    agent = _agent_name()
    environment = os.environ.get(
        "OPENCLAW_DEPLOYMENT_ENVIRONMENT", os.environ.get("DEPLOYMENT_ENVIRONMENT", "production")
    )
    version = os.environ.get("OPENCLAW_SERVICE_VERSION", "dev")

    attrs: dict[str, Any] = {
        SERVICE_NAME: f"openclaw-livekit-{agent}" if agent != "unknown" else "openclaw-livekit",
        SERVICE_NAMESPACE: "openclaw",
        SERVICE_VERSION: version,
        SERVICE_INSTANCE_ID: _instance_id,
        DEPLOYMENT_ENVIRONMENT: environment,
        HOST_NAME: socket.gethostname(),
        PROCESS_PID: os.getpid(),
        "openclaw.agent": agent,
        "openclaw.platform": platform.platform(),
        "openclaw.python_version": platform.python_version(),
    }
    return Resource.create(attrs)


def _configure_exporters(provider: Any) -> list[str]:
    """Register one BatchSpanProcessor per configured exporter.

    Default = ``otlp`` (SigNoz-primary, established 2026-05-01). Set
    ``OPENCLAW_OTEL_EXPORTERS=langsmith`` to fan out to LangSmith
    (legacy) or ``OPENCLAW_OTEL_EXPORTERS=otlp,langsmith`` to dual-export
    while migrating.
    """
    requested = os.environ.get("OPENCLAW_OTEL_EXPORTERS", "otlp")
    names = [n.strip().lower() for n in requested.split(",") if n.strip()]
    if not names:
        names = ["otlp"]

    enabled: list[str] = []
    for name in names:
        try:
            if _add_exporter(provider, name):
                enabled.append(name)
        except Exception as exc:
            logger.warning("Failed to configure %r exporter: %s", name, exc)

    if _debug_enabled() and "console" not in enabled:
        # Always-on console fallback when debug is set.
        try:
            if _add_exporter(provider, "console"):
                enabled.append("console")
        except Exception as exc:
            logger.warning("Failed to configure console exporter: %s", exc)

    return enabled


def _add_exporter(provider: Any, name: str) -> bool:
    from opentelemetry.sdk.trace.export import BatchSpanProcessor

    if name == "langsmith":
        endpoint = os.environ.get("OTEL_EXPORTER_OTLP_ENDPOINT")
        headers = os.environ.get("OTEL_EXPORTER_OTLP_HEADERS")
        if not endpoint or not headers:
            logger.warning(
                "langsmith exporter requested but OTEL_EXPORTER_OTLP_ENDPOINT or "
                "OTEL_EXPORTER_OTLP_HEADERS is missing — skipping"
            )
            return False
        from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter

        provider.add_span_processor(
            BatchSpanProcessor(
                OTLPSpanExporter(),
                # Tight flush window for short voice jobs — see
                # docs/LANGSMITH.md "Flush on short jobs".
                max_queue_size=2048,
                max_export_batch_size=64,
                schedule_delay_millis=1000,
            )
        )
        return True

    if name == "phoenix":
        endpoint = os.environ.get("OPENCLAW_PHOENIX_ENDPOINT", "http://localhost:6006/v1/traces")
        headers = _parse_headers(os.environ.get("OPENCLAW_PHOENIX_HEADERS"))
        from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter

        provider.add_span_processor(
            BatchSpanProcessor(OTLPSpanExporter(endpoint=endpoint, headers=headers))
        )
        return True

    if name == "otlp":
        endpoint = os.environ.get("OPENCLAW_OTLP_ENDPOINT")
        headers = _parse_headers(os.environ.get("OPENCLAW_OTLP_HEADERS"))
        if not endpoint:
            logger.warning("otlp exporter requested but OPENCLAW_OTLP_ENDPOINT missing — skipping")
            return False
        from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter

        provider.add_span_processor(
            BatchSpanProcessor(OTLPSpanExporter(endpoint=endpoint, headers=headers))
        )
        return True

    if name == "console":
        from opentelemetry.sdk.trace.export import ConsoleSpanExporter, SimpleSpanProcessor

        provider.add_span_processor(SimpleSpanProcessor(ConsoleSpanExporter()))
        return True

    logger.warning("Unknown OTel exporter %r — skipping", name)
    return False


def _parse_headers(raw: str | None) -> dict[str, str] | None:
    if not raw:
        return None
    out: dict[str, str] = {}
    for part in raw.split(","):
        if "=" not in part:
            continue
        key, value = part.split("=", 1)
        key = key.strip()
        value = value.strip()
        if key:
            out[key] = value
    return out or None


def _install_http_instrumentation(provider: Any) -> None:
    """Auto-instrument outbound HTTP so Musubi / gateway / weather calls
    show up as ``http.client`` child spans under whatever tool is active.
    """
    try:
        from opentelemetry.instrumentation.aiohttp_client import AioHttpClientInstrumentor

        AioHttpClientInstrumentor().instrument(tracer_provider=provider)
        _debug("[OTEL-SETUP] aiohttp client instrumented")
    except ImportError:
        _debug("[OTEL-SETUP] aiohttp instrumentation unavailable (package not installed)")
    except Exception as exc:
        logger.debug("aiohttp instrumentation failed: %s", exc)

    try:
        from opentelemetry.instrumentation.requests import RequestsInstrumentor

        RequestsInstrumentor().instrument(tracer_provider=provider)
        _debug("[OTEL-SETUP] requests instrumented")
    except ImportError:
        _debug("[OTEL-SETUP] requests instrumentation unavailable (package not installed)")
    except Exception as exc:
        logger.debug("requests instrumentation failed: %s", exc)


def _logs_enabled() -> bool:
    """Logs ship via OTLP only when an OTLP-compatible backend is configured.
    LangSmith does NOT accept OTel logs, so we only enable the logs pipeline
    when a non-LangSmith exporter is in OPENCLAW_OTEL_EXPORTERS or when
    OPENCLAW_OTEL_LOGS_ENABLED=true is set explicitly.
    """
    explicit = os.environ.get("OPENCLAW_OTEL_LOGS_ENABLED", "").lower()
    if explicit in ("true", "1", "yes"):
        return True
    if explicit in ("false", "0", "no"):
        return False
    exporters = os.environ.get("OPENCLAW_OTEL_EXPORTERS", "otlp").lower()
    return any(name.strip() in {"otlp", "phoenix", "console"} for name in exporters.split(","))


def _install_logs_pipeline(resource: Any) -> None:
    """Bridge stdlib ``logging`` to OTel + ship records via OTLP.

    Two effects:
      1. ``LoggingInstrumentor`` injects ``otelTraceID`` / ``otelSpanID`` /
         ``otelServiceName`` into every Python LogRecord so existing JSON
         log files cross-correlate with traces.
      2. An ``LoggingHandler`` fans every record into the OTel logs SDK,
         which batches and exports via OTLPLogExporter to whatever backend
         is configured (SigNoz, Loki via collector, Datadog, ...).
    """
    global _logger_provider

    try:
        from opentelemetry._logs import set_logger_provider
        from opentelemetry.exporter.otlp.proto.http._log_exporter import OTLPLogExporter
        from opentelemetry.instrumentation.logging import LoggingInstrumentor
        from opentelemetry.sdk._logs import LoggerProvider, LoggingHandler
        from opentelemetry.sdk._logs.export import BatchLogRecordProcessor
    except ImportError as exc:
        _debug(f"[OTEL-SETUP] logs pipeline unavailable: {exc}")
        return

    endpoint = os.environ.get("OPENCLAW_OTLP_LOGS_ENDPOINT") or os.environ.get(
        "OPENCLAW_OTLP_ENDPOINT"
    )
    if endpoint and "/v1/" not in endpoint:
        endpoint = endpoint.rstrip("/") + "/v1/logs"
    elif endpoint and endpoint.endswith("/v1/traces"):
        endpoint = endpoint[: -len("/v1/traces")] + "/v1/logs"

    headers = _parse_headers(
        os.environ.get("OPENCLAW_OTLP_LOGS_HEADERS") or os.environ.get("OPENCLAW_OTLP_HEADERS")
    )

    try:
        provider = LoggerProvider(resource=resource)
        if endpoint:
            exporter = OTLPLogExporter(endpoint=endpoint, headers=headers)
        else:
            exporter = OTLPLogExporter(headers=headers)
        provider.add_log_record_processor(BatchLogRecordProcessor(exporter))
        set_logger_provider(provider)
        _logger_provider = provider

        # Attach an OTel handler at the root so every logger downstream
        # (including livekit.agents.*, sdk.*, app code) ships its records.
        otel_handler = LoggingHandler(level=logging.NOTSET, logger_provider=provider)
        logging.getLogger().addHandler(otel_handler)

        # Inject trace_id/span_id into LogRecord attributes for use in
        # local JSON formatters too — operators can grep agent log
        # files by the same trace ID they see in LangSmith / SigNoz.
        LoggingInstrumentor().instrument(set_logging_format=False)

        _debug(f"[OTEL-SETUP] logs pipeline enabled (endpoint={endpoint or 'default'})")
    except Exception as exc:
        logger.warning("OTel logs pipeline failed to initialize: %s", exc)


def _metrics_enabled() -> bool:
    """Metrics pipeline auto-enables for OTLP-compatible backends.

    The SigNoz LiveKit dashboard's "HTTP Request Duration" panel reads
    ``http.client.duration`` histograms — only published when the OTel
    metrics SDK is wired AND an HTTP client instrumentor is configured
    to emit metrics (it auto-emits them once a meter provider exists).
    Honour ``OPENCLAW_OTEL_METRICS_ENABLED`` for explicit override.
    """
    explicit = os.environ.get("OPENCLAW_OTEL_METRICS_ENABLED", "").lower()
    if explicit in ("true", "1", "yes"):
        return True
    if explicit in ("false", "0", "no"):
        return False
    exporters = os.environ.get("OPENCLAW_OTEL_EXPORTERS", "otlp").lower()
    return any(name.strip() in {"otlp", "phoenix", "console"} for name in exporters.split(","))


def _install_metrics_pipeline(resource: Any) -> None:
    """Wire OTel metrics SDK + OTLP exporter so SigNoz dashboards work.

    Lights up:
      * ``http.client.duration`` (auto from aiohttp/requests instrumentors),
      * ``system.*`` host metrics (CPU, mem, network, disk),
      * any custom counters/histograms downstream code records via
        ``opentelemetry.metrics.get_meter("openclaw")``.
    """
    global _meter_provider

    try:
        from opentelemetry import metrics as otel_metrics
        from opentelemetry.exporter.otlp.proto.http.metric_exporter import OTLPMetricExporter
        from opentelemetry.sdk.metrics import MeterProvider
        from opentelemetry.sdk.metrics.export import PeriodicExportingMetricReader
    except ImportError as exc:
        _debug(f"[OTEL-SETUP] metrics pipeline unavailable: {exc}")
        return

    endpoint = os.environ.get("OPENCLAW_OTLP_METRICS_ENDPOINT") or os.environ.get(
        "OPENCLAW_OTLP_ENDPOINT"
    )
    if endpoint and "/v1/" not in endpoint:
        endpoint = endpoint.rstrip("/") + "/v1/metrics"
    elif endpoint and endpoint.endswith("/v1/traces"):
        endpoint = endpoint[: -len("/v1/traces")] + "/v1/metrics"

    headers = _parse_headers(
        os.environ.get("OPENCLAW_OTLP_METRICS_HEADERS") or os.environ.get("OPENCLAW_OTLP_HEADERS")
    )

    try:
        if endpoint:
            exporter = OTLPMetricExporter(endpoint=endpoint, headers=headers)
        else:
            exporter = OTLPMetricExporter(headers=headers)
        reader = PeriodicExportingMetricReader(exporter, export_interval_millis=15000)
        provider = MeterProvider(resource=resource, metric_readers=[reader])
        otel_metrics.set_meter_provider(provider)
        _meter_provider = provider

        # Optional system-metrics instrumentor — provides CPU/memory/disk/
        # network gauges when the package is installed. Silently skip when
        # missing so the metrics pipeline still ships application metrics.
        try:
            from opentelemetry.instrumentation.system_metrics import SystemMetricsInstrumentor

            SystemMetricsInstrumentor().instrument(meter_provider=provider)
            _debug("[OTEL-SETUP] system metrics instrumented")
        except ImportError:
            _debug("[OTEL-SETUP] system metrics unavailable (package not installed)")
        except Exception as exc:
            logger.debug("system metrics instrumentation failed: %s", exc)

        _debug(f"[OTEL-SETUP] metrics pipeline enabled (endpoint={endpoint or 'default'})")
    except Exception as exc:
        logger.warning("OTel metrics pipeline failed to initialize: %s", exc)


__all__ = [
    "attach_current_span_metadata",
    "force_flush_otel_tracing",
    "setup_otel_tracing",
    "shutdown_otel_tracing",
    "wire_otel_shutdown_flush",
]
