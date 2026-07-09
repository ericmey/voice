"""OpenTelemetry tracing, logs, and metrics for the LiveKit voice agents.

The configured backend is any OTLP/HTTP-compatible collector, commonly
Grafana + Loki + Tempo + Mimir behind an OTel Collector. The setup relies
on LiveKit Agents 1.5+ emitting native ``gen_ai.*``
semantic-convention attributes plus ``lk.*`` LiveKit-specific
attributes. We add no span enrichment beyond:

* :class:`NoiseSpanFilter` — drops the few LiveKit spans that are pure
  UI noise (``agent_speaking``, ``user_speaking``, ``drain_agent_activity``,
  ``on_enter``, ``on_exit``) so the trace tree stays readable. The Tempo /
  Grafana stack has no built-in span-name dropper; this is the minimum
  custom code needed to keep the call view focused on conversation
  content.
* :func:`attach_current_span_metadata` — stamps SIP / caller identity
  onto the active ``agent_session`` span as standard OTel SemConv
  attributes (``session.id``, ``enduser.id``) plus a small set of
  telephony-routing fields (``voice.dialed_number`` /
  ``voice.caller_source`` / ``voice.lk_job_id``) not covered by
  SemConv.

The exporter speaks generic OTLP/HTTP, so any OTLP backend works
without code changes — point ``VOICE_OTLP_ENDPOINT`` at a different
collector if the topology ever shifts.

Configuration:

* ``VOICE_OTEL_ENABLED=true`` — master switch.
* ``VOICE_OTLP_ENDPOINT`` / ``VOICE_OTLP_HEADERS`` — OTLP/HTTP
  traces endpoint (default:
  ``http://localhost:4318/v1/traces``) and any auth headers required by
  the backend.
* ``VOICE_OTEL_DEBUG=true`` — adds a ConsoleSpanExporter for local diag.
* ``VOICE_OTEL_HTTP_INSTRUMENTATION=false`` — disable HTTP auto-instr.
* ``VOICE_OTEL_VERBOSE=true`` — keep the noise spans in the trace tree.
* ``VOICE_OTEL_LOGS_ENABLED`` / ``VOICE_OTEL_METRICS_ENABLED`` —
  explicit overrides (auto-on alongside an OTLP exporter).

Setup MUST happen before ``AgentServer()`` is instantiated; LiveKit
caches the tracer provider at server-construction time.
"""

from __future__ import annotations

import atexit
import logging
import os
import platform
import socket
import uuid
from typing import Any

from opentelemetry.sdk.trace import ReadableSpan, Span, SpanProcessor

logger = logging.getLogger("voice.tracing")

_initialized = False
_provider: Any | None = None
_logger_provider: Any | None = None
_meter_provider: Any | None = None
_atexit_registered = False
_instance_id = uuid.uuid4().hex


# Spans that are pure UI noise. Filtered out unless VOICE_OTEL_VERBOSE
# is set. ``agent_speaking`` / ``user_speaking`` mark TTS playback and
# user audio capture, not conversation events; ``on_enter`` / ``on_exit``
# / ``drain_agent_activity`` are framework lifecycle hooks. None contain
# fields any of our LiveKit dashboards in Grafana query.
_NOISE_SPAN_NAMES = frozenset(
    {
        "agent_speaking",
        "user_speaking",
        "drain_agent_activity",
        "on_enter",
        "on_exit",
    }
)


class NoiseSpanFilter(SpanProcessor):
    """Drop LiveKit framework-noise spans before they reach the exporter.

    Wraps another :class:`SpanProcessor` and forwards everything except
    spans whose name is in :data:`_NOISE_SPAN_NAMES`. Honours
    ``VOICE_OTEL_VERBOSE=true`` to disable filtering for deep dives.
    """

    def __init__(self, downstream: SpanProcessor) -> None:
        self._downstream = downstream

    def on_start(self, span: Span, parent_context: Any = None) -> None:
        self._downstream.on_start(span, parent_context)

    def on_end(self, span: ReadableSpan) -> None:
        if not _verbose_telemetry_enabled() and span.name in _NOISE_SPAN_NAMES:
            return
        self._downstream.on_end(span)

    def shutdown(self) -> None:
        self._downstream.shutdown()

    def force_flush(self, timeout_millis: int = 30000) -> bool:
        return self._downstream.force_flush(timeout_millis)


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def setup_otel_tracing() -> None:
    """Wire OTel tracing if enabled. Idempotent.

    Reads ``VOICE_OTEL_ENABLED``. Configures the TracerProvider with
    one BatchSpanProcessor wrapped in :class:`NoiseSpanFilter`, then
    publishes the provider to both the global OTel registry and
    LiveKit's dynamic tracer wrapper.
    """
    global _initialized, _provider, _atexit_registered

    pid = os.getpid()
    _debug(f"[OTEL-SETUP] pid={pid} entered setup_otel_tracing")

    if _initialized:
        _debug(f"[OTEL-SETUP] pid={pid} already initialized, skipping")
        return

    if not _otel_enabled():
        _debug(f"[OTEL-SETUP] pid={pid} VOICE_OTEL_ENABLED off — disabled")
        return

    try:
        from livekit.agents.telemetry import set_tracer_provider as set_livekit_tracer_provider
        from opentelemetry import trace as otel_trace
        from opentelemetry.sdk.trace import TracerProvider
    except ImportError as exc:
        _debug(f"[OTEL-SETUP] pid={pid} ImportError: {exc} — disabled")
        logger.warning(
            "VOICE_OTEL_ENABLED=true but OTel deps not installed (%s) — disabled. "
            "Run `uv sync` from the repository root to refresh the workspace environment.",
            exc,
        )
        return

    provider = TracerProvider(resource=_build_resource())

    if not _add_otlp_exporter(provider):
        logger.warning("OTEL tracing enabled but no OTLP exporter configured — disabled")
        return

    if _debug_enabled():
        _add_console_exporter(provider)

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
    _debug(f"[OTEL-SETUP] pid={pid} ENABLED resource={dict(provider.resource.attributes)}")
    logger.info("OTel tracing enabled (OTLP/HTTP)")


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

    async def _flush_otel(_reason: str = "") -> None:
        force_flush_otel_tracing(timeout_millis)

    try:
        add_shutdown_callback(_flush_otel)
    except Exception as exc:
        logger.warning("OTel shutdown flush hook registration failed: %s", exc)


def attach_current_span_metadata(
    *,
    session_id: str | None = None,
    enduser_id: str | None = None,
    dialed_number: str | None = None,
    caller_source: str | None = None,
    lk_job_id: str | None = None,
) -> None:
    """Stamp SIP / caller identity onto the active ``agent_session`` span.

    LiveKit's ``AgentSession.start()`` creates an ``agent_session`` span
    and attaches it as the active OTel context (see
    ``livekit/agents/voice/agent_session.py:653-660``). Agent
    entrypoints call this immediately after ``await session.start(...)``
    so the call's root span carries:

    * ``session.id`` — SIP Call-ID (OTel SemConv standard).
    * ``enduser.id`` — caller phone number in E.164 (OTel SemConv).
    * ``voice.dialed_number`` — which DID the caller dialed.
    * ``voice.caller_source`` — twilio / sip / livekit-cloud / ...
    * ``voice.lk_job_id`` — LiveKit job ID for cross-log correlation.

    Operators filter Traces by ``service.name`` plus any of the above on
    the root ``agent_session`` span, then drill into the trace tree
    (``agent_turn`` / ``llm_request`` / ``tts_node`` / ``function_tool``).
    """
    try:
        from opentelemetry import trace as otel_trace
    except ImportError:
        return

    span = otel_trace.get_current_span()
    if span is None or not span.is_recording():
        return

    if session_id:
        span.set_attribute("session.id", str(session_id))
    if enduser_id:
        span.set_attribute("enduser.id", str(enduser_id))
    if dialed_number:
        span.set_attribute("voice.dialed_number", str(dialed_number))
    if caller_source:
        span.set_attribute("voice.caller_source", str(caller_source))
    if lk_job_id:
        span.set_attribute("voice.lk_job_id", str(lk_job_id))


# ---------------------------------------------------------------------------
# Internals
# ---------------------------------------------------------------------------


def _otel_enabled() -> bool:
    return os.environ.get("VOICE_OTEL_ENABLED", "").lower() in ("true", "1", "yes")


def _http_instrumentation_enabled() -> bool:
    return os.environ.get("VOICE_OTEL_HTTP_INSTRUMENTATION", "true").lower() not in (
        "false",
        "0",
        "no",
    )


def _debug_enabled() -> bool:
    return os.environ.get("VOICE_OTEL_DEBUG", "").lower() in ("true", "1", "yes")


def _verbose_telemetry_enabled() -> bool:
    return os.environ.get("VOICE_OTEL_VERBOSE", "").lower() in ("true", "1", "yes")


def _debug(message: str) -> None:
    if not _debug_enabled():
        return
    import sys as _sys

    print(message, file=_sys.stderr, flush=True)


def _agent_name() -> str:
    return (os.environ.get("VOICE_AGENT_NAME") or "").strip().lower() or "unknown"


def _build_resource() -> Any:
    """Identify this process to the OTLP backend."""
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
        "VOICE_DEPLOYMENT_ENVIRONMENT",
        os.environ.get("DEPLOYMENT_ENVIRONMENT", "local"),
    )
    version = os.environ.get("VOICE_SERVICE_VERSION", "dev")

    attrs: dict[str, Any] = {
        SERVICE_NAME: f"voice-{agent}" if agent != "unknown" else "voice",
        SERVICE_NAMESPACE: "voice",
        SERVICE_VERSION: version,
        SERVICE_INSTANCE_ID: _instance_id,
        DEPLOYMENT_ENVIRONMENT: environment,
        HOST_NAME: socket.gethostname(),
        PROCESS_PID: os.getpid(),
        "voice.platform": platform.platform(),
        "voice.python_version": platform.python_version(),
    }
    return Resource.create(attrs)


def _add_otlp_exporter(provider: Any) -> bool:
    from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
    from opentelemetry.sdk.trace.export import BatchSpanProcessor

    endpoint = os.environ.get("VOICE_OTLP_ENDPOINT")
    headers = _parse_headers(os.environ.get("VOICE_OTLP_HEADERS"))
    if not endpoint:
        logger.warning("VOICE_OTLP_ENDPOINT not set — OTLP exporter disabled")
        return False

    batch = BatchSpanProcessor(OTLPSpanExporter(endpoint=endpoint, headers=headers))
    provider.add_span_processor(NoiseSpanFilter(batch))
    return True


def _add_console_exporter(provider: Any) -> None:
    from opentelemetry.sdk.trace.export import ConsoleSpanExporter, SimpleSpanProcessor

    provider.add_span_processor(NoiseSpanFilter(SimpleSpanProcessor(ConsoleSpanExporter())))


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
    """Auto-instrument outbound HTTP for ``http.client`` spans + duration metric.

    ``http.client.duration`` is the metric our LiveKit Grafana dashboard's
    "HTTP Request Duration" panel reads. Three instrumentors cover the
    libraries the LiveKit plugins use:

    * **httpx** — openai plugin, google.genai SDK
    * **aiohttp-client** — elevenlabs plugin
    * **requests** — Twilio SDK and gateway HTTP calls
    """
    try:
        from opentelemetry.instrumentation.httpx import HTTPXClientInstrumentor

        HTTPXClientInstrumentor().instrument(tracer_provider=provider)
        _debug("[OTEL-SETUP] httpx instrumented")
    except ImportError:
        _debug("[OTEL-SETUP] httpx instrumentation unavailable (package not installed)")
    except Exception as exc:
        logger.debug("httpx instrumentation failed: %s", exc)

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
    explicit = os.environ.get("VOICE_OTEL_LOGS_ENABLED", "").lower()
    if explicit in ("true", "1", "yes"):
        return True
    if explicit in ("false", "0", "no"):
        return False
    return bool(os.environ.get("VOICE_OTLP_ENDPOINT"))


# Loggers whose records must never enter the OTel logs pipeline. Shipping
# them creates a feedback loop: the OTLP HTTP exporter POSTs to the
# collector, urllib3 logs the POST at DEBUG, the root LoggingHandler
# captures that DEBUG line, ships it via OTLP, which causes another
# POST. Throttled only by BatchLogRecordProcessor flush cadence — at
# DEBUG verbosity it produces ~1k records/sec of pure self-traffic.
_OTEL_INTERNAL_LOGGER_PREFIXES = (
    "urllib3",
    "opentelemetry.exporter",
    "opentelemetry.sdk._logs",
    "opentelemetry.sdk.trace.export",
    "opentelemetry.sdk.metrics.export",
)


class _OtelInternalLoopFilter(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        return not record.name.startswith(_OTEL_INTERNAL_LOGGER_PREFIXES)


def _install_logs_pipeline(resource: Any) -> None:
    """Bridge stdlib ``logging`` to OTel + ship records via OTLP.

    Two effects:
      1. ``LoggingInstrumentor`` injects ``otelTraceID`` / ``otelSpanID`` /
         ``otelServiceName`` into every Python LogRecord so JSON log files
         cross-correlate with traces.
      2. An ``LoggingHandler`` fans every record into the OTel logs SDK,
         which batches and exports via OTLPLogExporter to the OTLP backend.

    The handler is filtered to exclude OTel-internal HTTP/exporter
    loggers (see :data:`_OTEL_INTERNAL_LOGGER_PREFIXES`) — without that
    filter the exporter logs itself and the pipeline runs away.
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

    endpoint = os.environ.get("VOICE_OTLP_LOGS_ENDPOINT") or os.environ.get("VOICE_OTLP_ENDPOINT")
    if endpoint and "/v1/" not in endpoint:
        endpoint = endpoint.rstrip("/") + "/v1/logs"
    elif endpoint and endpoint.endswith("/v1/traces"):
        endpoint = endpoint[: -len("/v1/traces")] + "/v1/logs"

    headers = _parse_headers(
        os.environ.get("VOICE_OTLP_LOGS_HEADERS") or os.environ.get("VOICE_OTLP_HEADERS")
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

        otel_handler = LoggingHandler(level=logging.NOTSET, logger_provider=provider)
        otel_handler.addFilter(_OtelInternalLoopFilter())
        logging.getLogger().addHandler(otel_handler)

        # Belt-and-suspenders: even if some other handler (file, stderr)
        # is attached at DEBUG, the OTLP HTTP transport's per-POST chatter
        # is never useful operational signal — drop it at the source.
        logging.getLogger("urllib3").setLevel(logging.WARNING)
        logging.getLogger("urllib3.connectionpool").setLevel(logging.WARNING)

        LoggingInstrumentor().instrument(set_logging_format=False)

        _debug(f"[OTEL-SETUP] logs pipeline enabled (endpoint={endpoint or 'default'})")
    except Exception as exc:
        logger.warning("OTel logs pipeline failed to initialize: %s", exc)


def _metrics_enabled() -> bool:
    explicit = os.environ.get("VOICE_OTEL_METRICS_ENABLED", "").lower()
    if explicit in ("true", "1", "yes"):
        return True
    if explicit in ("false", "0", "no"):
        return False
    return bool(os.environ.get("VOICE_OTLP_ENDPOINT"))


def _install_metrics_pipeline(resource: Any) -> None:
    """Wire OTel metrics SDK + OTLP exporter so Grafana dashboards (Mimir-backed) work.

    Lights up:
      * ``http.client.duration`` (auto from httpx/aiohttp/requests
        instrumentors)
      * ``system.*`` host metrics (CPU, mem, network, disk) from
        :class:`SystemMetricsInstrumentor`
      * any custom counters/histograms downstream code records via
        ``opentelemetry.metrics.get_meter("voice")``.
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

    endpoint = os.environ.get("VOICE_OTLP_METRICS_ENDPOINT") or os.environ.get(
        "VOICE_OTLP_ENDPOINT"
    )
    if endpoint and "/v1/" not in endpoint:
        endpoint = endpoint.rstrip("/") + "/v1/metrics"
    elif endpoint and endpoint.endswith("/v1/traces"):
        endpoint = endpoint[: -len("/v1/traces")] + "/v1/metrics"

    headers = _parse_headers(
        os.environ.get("VOICE_OTLP_METRICS_HEADERS") or os.environ.get("VOICE_OTLP_HEADERS")
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
    "NoiseSpanFilter",
    "attach_current_span_metadata",
    "force_flush_otel_tracing",
    "setup_otel_tracing",
    "shutdown_otel_tracing",
    "wire_otel_shutdown_flush",
]
