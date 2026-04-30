"""Tests for the LangSmith tracing setup module.

Covers the gating contract: ``setup_langsmith_tracing`` must be a hard
no-op when ``LANGSMITH_TRACING`` is unset/false so unit tests, CI, and
local dev stay hermetic. When the flag is on, it must wire LiveKit's
tracer provider with our vendored ``LangSmithSpanProcessor``.
"""

from __future__ import annotations

import os
from unittest.mock import patch

from sdk import tracing


def _reset_module_state() -> None:
    """Tracing setup is module-level idempotent — reset between tests."""
    tracing._initialized = False


def test_setup_is_noop_when_env_var_unset(monkeypatch) -> None:
    """No env var = no tracer provider mutation, no exceptions."""
    monkeypatch.delenv("LANGSMITH_TRACING", raising=False)
    _reset_module_state()

    with patch("livekit.agents.telemetry.set_tracer_provider") as mock_set:
        tracing.setup_langsmith_tracing()

    mock_set.assert_not_called()
    assert tracing._initialized is False


def test_setup_is_noop_when_env_var_false(monkeypatch) -> None:
    """Explicitly disabled is the same as unset — provider stays untouched."""
    monkeypatch.setenv("LANGSMITH_TRACING", "false")
    _reset_module_state()

    with patch("livekit.agents.telemetry.set_tracer_provider") as mock_set:
        tracing.setup_langsmith_tracing()

    mock_set.assert_not_called()


def test_setup_is_noop_when_otel_endpoint_missing(monkeypatch) -> None:
    """Tracing requested but no OTel endpoint → degrade quietly with a warning,
    not a crash. Operator forgot a half-step in the env file; production must
    keep serving calls regardless."""
    monkeypatch.setenv("LANGSMITH_TRACING", "true")
    monkeypatch.delenv("OTEL_EXPORTER_OTLP_ENDPOINT", raising=False)
    monkeypatch.delenv("OTEL_EXPORTER_OTLP_HEADERS", raising=False)
    _reset_module_state()

    with patch("livekit.agents.telemetry.set_tracer_provider") as mock_set:
        tracing.setup_langsmith_tracing()

    mock_set.assert_not_called()


def test_setup_wires_provider_when_fully_configured(monkeypatch) -> None:
    """Happy path — flag on + endpoint + headers → tracer provider is set
    with our processor attached. Asserts the integration point LiveKit relies
    on (``set_tracer_provider``) is actually called."""
    monkeypatch.setenv("LANGSMITH_TRACING", "true")
    monkeypatch.setenv("OTEL_EXPORTER_OTLP_ENDPOINT", "https://api.smith.langchain.com/otel")
    monkeypatch.setenv(
        "OTEL_EXPORTER_OTLP_HEADERS", "x-api-key=test-key,Langsmith-Project=test-project"
    )
    _reset_module_state()

    # Patch BEFORE setup_langsmith_tracing's lazy import of livekit.agents.telemetry
    # so the call lands on our mock instead of the real LiveKit hook.
    with patch("livekit.agents.telemetry.set_tracer_provider") as mock_set:
        tracing.setup_langsmith_tracing()

    mock_set.assert_called_once()
    assert tracing._initialized is True


def test_setup_is_idempotent(monkeypatch) -> None:
    """Multiple calls (re-imports, accidental double-init) must not register
    the processor twice — we'd duplicate every span."""
    monkeypatch.setenv("LANGSMITH_TRACING", "true")
    monkeypatch.setenv("OTEL_EXPORTER_OTLP_ENDPOINT", "https://api.smith.langchain.com/otel")
    monkeypatch.setenv("OTEL_EXPORTER_OTLP_HEADERS", "x-api-key=k,Langsmith-Project=p")
    _reset_module_state()

    with patch("livekit.agents.telemetry.set_tracer_provider") as mock_set:
        tracing.setup_langsmith_tracing()
        tracing.setup_langsmith_tracing()  # second call — must be a no-op
        tracing.setup_langsmith_tracing()  # third call too

    assert mock_set.call_count == 1


def teardown_module(module) -> None:  # noqa: ARG001
    """Don't leave the module flagged as initialised — other test modules
    that import sdk.env (which calls setup) would skip their own wiring."""
    _reset_module_state()
    # Also clear env vars we may have set.
    for k in (
        "LANGSMITH_TRACING",
        "OTEL_EXPORTER_OTLP_ENDPOINT",
        "OTEL_EXPORTER_OTLP_HEADERS",
    ):
        os.environ.pop(k, None)
