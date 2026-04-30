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
    # Diagnostic prints go straight to stderr so they bypass any
    # logging-config ambiguity in the spawned job subprocess. Will
    # remove once we've confirmed which branch fires in production.
    import sys as _sys
    _pid = os.getpid()
    print(f"[TRACING-SETUP] pid={_pid} entered setup_langsmith_tracing", file=_sys.stderr, flush=True)

    global _initialized
    if _initialized:
        print(f"[TRACING-SETUP] pid={_pid} already initialized, skipping", file=_sys.stderr, flush=True)
        return

    tracing_env = os.environ.get("LANGSMITH_TRACING", "")
    if tracing_env.lower() not in ("true", "1", "yes"):
        print(
            f"[TRACING-SETUP] pid={_pid} LANGSMITH_TRACING={tracing_env!r} — disabled",
            file=_sys.stderr, flush=True,
        )
        return

    endpoint = os.environ.get("OTEL_EXPORTER_OTLP_ENDPOINT")
    headers = os.environ.get("OTEL_EXPORTER_OTLP_HEADERS")
    print(
        f"[TRACING-SETUP] pid={_pid} endpoint={'SET' if endpoint else 'MISSING'} "
        f"headers={'SET' if headers else 'MISSING'}",
        file=_sys.stderr, flush=True,
    )
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
        print(
            f"[TRACING-SETUP] pid={_pid} ImportError: {exc} — disabled",
            file=_sys.stderr, flush=True,
        )
        logger.warning(
            "LANGSMITH_TRACING=true but tracing deps not installed (%s) — disabled. "
            "Install with: uv sync --extra tracing",
            exc,
        )
        return

    provider = TracerProvider()
    processor = LangSmithSpanProcessor()
    provider.add_span_processor(processor)
    set_tracer_provider(provider)

    _initialized = True
    print(
        f"[TRACING-SETUP] pid={_pid} ENABLED — provider={type(provider).__name__} "
        f"processor={type(processor).__name__} downstream={type(processor.downstream).__name__}",
        file=_sys.stderr, flush=True,
    )
    logger.info("LangSmith tracing enabled (endpoint=%s)", endpoint)
