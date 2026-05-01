"""LangSmith OpenTelemetry tracing setup for LiveKit agents.

Wires the vendored :class:`LangSmithSpanProcessor` into LiveKit's OTel
tracer provider so every span LiveKit emits — session, turn, LLM,
tool, STT, TTS — flows to LangSmith for diagnosis and replay.

Gated on ``LANGSMITH_TRACING=true``. When the gate is off the function
is a complete no-op so unit tests + CI stay hermetic.

Usage from the agent process::

    from sdk.env import load_env
    load_env()  # also calls setup_langsmith_tracing()

The setup MUST happen before ``AgentServer()`` is instantiated; LiveKit
caches the tracer provider at server-construction time. We hook it via
``load_env()`` because every agent (Nyla, Aoi, Party) calls that at
module-top before anything LiveKit-related touches the tracer.
"""

from __future__ import annotations

import atexit
import logging
import os
from typing import Any

logger = logging.getLogger("openclaw-livekit.tracing")

_initialized = False
_provider: Any | None = None
_atexit_registered = False


def _agent_langsmith_project() -> str | None:
    explicit = os.environ.get("LANGSMITH_PROJECT")
    if explicit:
        return explicit

    agent_name = (os.environ.get("OPENCLAW_AGENT_NAME") or "").strip().lower()
    if not agent_name:
        return None
    return {
        "aoi": "Aoi",
        "nyla": "Nyla",
        "party": "Party",
    }.get(agent_name, agent_name.title())


def _headers_with_langsmith_project(headers: str, project: str | None) -> str:
    if not project:
        return headers

    parts = [part for part in headers.split(",") if part]
    parts = [part for part in parts if not part.lower().startswith("langsmith-project=")]
    parts.append(f"Langsmith-Project={project}")
    return ",".join(parts)


def _debug_enabled() -> bool:
    return os.environ.get("LANGSMITH_PROCESSOR_DEBUG", "").lower() in ("true", "1", "yes")


def _debug(message: str) -> None:
    if not _debug_enabled():
        return
    import sys as _sys

    print(message, file=_sys.stderr, flush=True)


def setup_langsmith_tracing() -> None:
    """Wire LangSmith OTel tracing if ``LANGSMITH_TRACING=true``.

    Idempotent — safe to call multiple times. Subsequent calls return
    immediately without re-registering the tracer provider.
    """
    _pid = os.getpid()
    _debug(f"[TRACING-SETUP] pid={_pid} entered setup_langsmith_tracing")

    global _initialized, _provider, _atexit_registered
    if _initialized:
        _debug(f"[TRACING-SETUP] pid={_pid} already initialized, skipping")
        return

    tracing_env = os.environ.get("LANGSMITH_TRACING", "")
    if tracing_env.lower() not in ("true", "1", "yes"):
        _debug(f"[TRACING-SETUP] pid={_pid} LANGSMITH_TRACING={tracing_env!r} — disabled")
        return

    endpoint = os.environ.get("OTEL_EXPORTER_OTLP_ENDPOINT")
    headers = os.environ.get("OTEL_EXPORTER_OTLP_HEADERS")
    _debug(
        f"[TRACING-SETUP] pid={_pid} endpoint={'SET' if endpoint else 'MISSING'} "
        f"headers={'SET' if headers else 'MISSING'}"
    )
    if not endpoint or not headers:
        logger.warning(
            "LANGSMITH_TRACING=true but OTEL_EXPORTER_OTLP_ENDPOINT or "
            "OTEL_EXPORTER_OTLP_HEADERS is missing — tracing disabled."
        )
        return
    project = _agent_langsmith_project()
    if project:
        headers = _headers_with_langsmith_project(headers, project)
        os.environ["OTEL_EXPORTER_OTLP_HEADERS"] = headers
        _debug(f"[TRACING-SETUP] pid={_pid} LangSmith project={project!r}")

    # Imports inside the function so the SDK package stays importable on
    # boxes that haven't installed the OTel exporter yet (CI, local
    # dev without tracing extras).
    try:
        from livekit.agents.telemetry import set_tracer_provider
        from opentelemetry import trace as otel_trace
        from opentelemetry.sdk.resources import SERVICE_NAME, Resource
        from opentelemetry.sdk.trace import TracerProvider

        from sdk.langsmith_processor import LangSmithSpanProcessor
    except ImportError as exc:
        _debug(f"[TRACING-SETUP] pid={_pid} ImportError: {exc} — disabled")
        logger.warning(
            "LANGSMITH_TRACING=true but tracing deps not installed (%s) — disabled. "
            "Run `uv sync` from the repository root to refresh the workspace environment.",
            exc,
        )
        return

    provider = TracerProvider(resource=Resource.create({SERVICE_NAME: "openclaw-livekit"}))
    processor = LangSmithSpanProcessor()
    provider.add_span_processor(processor)
    # LiveKit uses its own dynamic tracer wrapper. The global provider is
    # still needed for any OpenClaw spans emitted outside LiveKit internals.
    otel_trace.set_tracer_provider(provider)
    set_tracer_provider(provider)

    _provider = provider
    if not _atexit_registered:
        atexit.register(shutdown_langsmith_tracing)
        _atexit_registered = True
    _initialized = True
    _debug(
        f"[TRACING-SETUP] pid={_pid} ENABLED — provider={type(provider).__name__} "
        f"processor={type(processor).__name__} downstream={type(processor.downstream).__name__}"
    )
    logger.info("LangSmith tracing enabled (endpoint=%s)", endpoint)


def force_flush_langsmith_tracing(timeout_millis: int = 10000) -> bool:
    """Flush pending LangSmith spans before a short-lived job exits."""
    if _provider is None:
        return True
    try:
        return bool(_provider.force_flush(timeout_millis))
    except Exception as exc:
        logger.warning("LangSmith tracing force_flush failed: %s", exc)
        return False


def shutdown_langsmith_tracing() -> None:
    """Best-effort process-exit shutdown for the OTel provider."""
    if _provider is None:
        return
    try:
        _provider.shutdown()
    except Exception as exc:
        logger.warning("LangSmith tracing shutdown failed: %s", exc)


def wire_langsmith_shutdown_flush(ctx: Any, timeout_millis: int = 10000) -> None:
    """Flush pending LangSmith spans when LiveKit tears down a job."""
    add_shutdown_callback = getattr(ctx, "add_shutdown_callback", None)
    if add_shutdown_callback is None:
        return
    try:
        add_shutdown_callback(lambda: force_flush_langsmith_tracing(timeout_millis))
    except Exception as exc:
        logger.warning("LangSmith shutdown flush hook registration failed: %s", exc)


def attach_current_span_metadata(**metadata: Any) -> None:
    """Attach call/job metadata to the active OTel span.

    LiveKit's ``AgentSession.start`` leaves the session span in the active
    OTel context for the job. Agent entrypoints call this after caller
    resolution so the root LangSmith run carries call_sid, caller, room,
    route/source, and other high-value filters.
    """
    try:
        from opentelemetry import trace as otel_trace
    except ImportError:
        return

    span = otel_trace.get_current_span()
    if span is None or not span.is_recording():
        return

    for key, value in metadata.items():
        if value is None or value == "":
            continue
        clean_key = key.replace(".", "_")
        span.set_attribute(f"langsmith.metadata.{clean_key}", str(value))
