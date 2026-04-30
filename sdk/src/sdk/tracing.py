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

import logging
import os

logger = logging.getLogger("openclaw-livekit.tracing")

_initialized = False


def setup_langsmith_tracing() -> None:
    """Wire LangSmith OTel tracing if ``LANGSMITH_TRACING=true``.

    Idempotent — safe to call multiple times. Subsequent calls return
    immediately without re-registering the tracer provider.
    """
    global _initialized
    if _initialized:
        return

    if os.environ.get("LANGSMITH_TRACING", "").lower() not in ("true", "1", "yes"):
        return

    endpoint = os.environ.get("OTEL_EXPORTER_OTLP_ENDPOINT")
    headers = os.environ.get("OTEL_EXPORTER_OTLP_HEADERS")
    if not endpoint or not headers:
        logger.warning(
            "LANGSMITH_TRACING=true but OTEL_EXPORTER_OTLP_ENDPOINT or "
            "OTEL_EXPORTER_OTLP_HEADERS is missing — tracing disabled."
        )
        return

    # Imports inside the function so the SDK package stays importable on
    # boxes that haven't installed the OTel exporter yet (CI, local
    # dev without tracing extras).
    try:
        from livekit.agents.telemetry import set_tracer_provider
        from opentelemetry.sdk.trace import TracerProvider

        from sdk.langsmith_processor import LangSmithSpanProcessor
    except ImportError as exc:
        logger.warning(
            "LANGSMITH_TRACING=true but tracing deps not installed (%s) — disabled. "
            "Install with: uv sync --extra tracing",
            exc,
        )
        return

    provider = TracerProvider()
    provider.add_span_processor(LangSmithSpanProcessor())
    set_tracer_provider(provider)

    _initialized = True
    logger.info("LangSmith tracing enabled (endpoint=%s)", endpoint)
