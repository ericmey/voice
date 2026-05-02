"""Tests for :mod:`sdk.tracing` — SigNoz-only OTel setup.

Pin the contract:

* ``setup_otel_tracing`` is a hard no-op unless
  ``OPENCLAW_OTEL_ENABLED=true`` AND ``OPENCLAW_OTLP_ENDPOINT`` is set.
* It wires LiveKit's tracer provider on success.
* :class:`NoiseSpanFilter` drops the named LiveKit framework spans
  unless ``OPENCLAW_OTEL_VERBOSE`` is set.
* :func:`attach_current_span_metadata` writes OTel-SemConv keys
  (``session.id``, ``enduser.id``) plus a small set of telephony-routing
  ``openclaw.*`` keys onto the active span.
"""

from __future__ import annotations

import os
from types import SimpleNamespace
from typing import cast
from unittest.mock import MagicMock, patch

from opentelemetry.sdk.trace import ReadableSpan, Span

from sdk import tracing


def _reset_module_state() -> None:
    """Tracing setup is module-level idempotent — reset between tests."""
    tracing._initialized = False
    tracing._provider = None
    tracing._logger_provider = None
    tracing._meter_provider = None


# ---------------------------------------------------------------------------
# setup gating
# ---------------------------------------------------------------------------


def test_setup_is_noop_when_env_var_unset(monkeypatch) -> None:
    monkeypatch.delenv("OPENCLAW_OTEL_ENABLED", raising=False)
    _reset_module_state()

    with patch("livekit.agents.telemetry.set_tracer_provider") as mock_set:
        tracing.setup_otel_tracing()

    mock_set.assert_not_called()
    assert tracing._initialized is False


def test_setup_is_noop_when_env_var_false(monkeypatch) -> None:
    monkeypatch.setenv("OPENCLAW_OTEL_ENABLED", "false")
    _reset_module_state()

    with patch("livekit.agents.telemetry.set_tracer_provider") as mock_set:
        tracing.setup_otel_tracing()

    mock_set.assert_not_called()


def test_setup_is_noop_when_otlp_endpoint_missing(monkeypatch) -> None:
    """Tracing requested but no OTLP endpoint configured → degrade quietly
    so production keeps serving calls. Operator forgot a half-step."""
    monkeypatch.setenv("OPENCLAW_OTEL_ENABLED", "true")
    monkeypatch.delenv("OPENCLAW_OTLP_ENDPOINT", raising=False)
    _reset_module_state()

    with patch("livekit.agents.telemetry.set_tracer_provider") as mock_set:
        tracing.setup_otel_tracing()

    mock_set.assert_not_called()


def test_setup_wires_provider_when_fully_configured(monkeypatch) -> None:
    """Happy path — enabled flag plus an OTLP endpoint must wire LiveKit's
    tracer provider. Asserts the integration point LiveKit relies on
    (``set_tracer_provider``) is actually called."""
    monkeypatch.setenv("OPENCLAW_OTEL_ENABLED", "true")
    monkeypatch.setenv("OPENCLAW_OTLP_ENDPOINT", "http://localhost:4318/v1/traces")
    _reset_module_state()

    with (
        patch("livekit.agents.telemetry.set_tracer_provider") as mock_set,
        patch("opentelemetry.trace.set_tracer_provider") as mock_global_set,
    ):
        tracing.setup_otel_tracing()

    mock_set.assert_called_once()
    mock_global_set.assert_called_once()
    assert tracing._initialized is True
    assert tracing._provider is not None


def test_setup_is_idempotent(monkeypatch) -> None:
    """Multiple calls (re-imports, accidental double-init) must not register
    the processor twice — duplicating spans would corrupt every dashboard."""
    monkeypatch.setenv("OPENCLAW_OTEL_ENABLED", "true")
    monkeypatch.setenv("OPENCLAW_OTLP_ENDPOINT", "http://localhost:4318/v1/traces")
    _reset_module_state()

    with (
        patch("livekit.agents.telemetry.set_tracer_provider") as mock_set,
        patch("opentelemetry.trace.set_tracer_provider"),
    ):
        tracing.setup_otel_tracing()
        tracing.setup_otel_tracing()
        tracing.setup_otel_tracing()

    assert mock_set.call_count == 1


# ---------------------------------------------------------------------------
# resource attributes
# ---------------------------------------------------------------------------


def test_resource_attributes_identify_agent(monkeypatch) -> None:
    monkeypatch.setenv("OPENCLAW_AGENT_NAME", "aoi")
    monkeypatch.setenv("OPENCLAW_DEPLOYMENT_ENVIRONMENT", "production")
    monkeypatch.setenv("OPENCLAW_SERVICE_VERSION", "0.4.2")

    resource = tracing._build_resource()
    attrs = dict(resource.attributes)

    assert attrs["service.name"] == "openclaw-livekit-aoi"
    assert attrs["service.namespace"] == "openclaw"
    assert attrs["service.version"] == "0.4.2"
    assert attrs["deployment.environment"] == "production"
    assert "service.instance.id" in attrs
    assert "host.name" in attrs


def test_resource_attributes_fallback_when_agent_name_unset(monkeypatch) -> None:
    """Without ``OPENCLAW_AGENT_NAME`` (e.g. in pytest's hermetic env),
    service.name must still be valid — falls back to a stable label."""
    monkeypatch.delenv("OPENCLAW_AGENT_NAME", raising=False)

    resource = tracing._build_resource()

    assert dict(resource.attributes)["service.name"] == "openclaw-livekit"


# ---------------------------------------------------------------------------
# NoiseSpanFilter
# ---------------------------------------------------------------------------


def _fake_span(name: str) -> ReadableSpan:
    """Stand-in for a :class:`ReadableSpan` — only the ``name`` attribute
    is read by :class:`NoiseSpanFilter`."""
    return cast(ReadableSpan, SimpleNamespace(name=name))


def test_noise_filter_drops_named_spans_by_default(monkeypatch) -> None:
    """Default mode: TTS-playback and lifecycle spans get suppressed so
    the SigNoz trace tree only shows conversation content."""
    monkeypatch.delenv("OPENCLAW_OTEL_VERBOSE", raising=False)
    downstream = MagicMock()
    f = tracing.NoiseSpanFilter(downstream)

    for name in (
        "agent_speaking",
        "user_speaking",
        "drain_agent_activity",
        "on_enter",
        "on_exit",
    ):
        f.on_end(_fake_span(name))

    downstream.on_end.assert_not_called()


def test_noise_filter_passes_through_meaningful_spans() -> None:
    """Anything the dashboards / operators care about must still hit the
    exporter: ``job_entrypoint``, ``agent_session``, ``agent_turn``,
    ``llm_request``, ``tts_node``, ``function_tool``, ``user_turn``."""
    downstream = MagicMock()
    f = tracing.NoiseSpanFilter(downstream)

    span_names = (
        "job_entrypoint",
        "agent_session",
        "agent_turn",
        "llm_request",
        "tts_node",
        "function_tool",
        "user_turn",
        "eou_detection",
    )
    for name in span_names:
        f.on_end(_fake_span(name))

    assert downstream.on_end.call_count == len(span_names)


def test_noise_filter_passes_everything_when_verbose(monkeypatch) -> None:
    """Operators flip ``OPENCLAW_OTEL_VERBOSE=true`` for deep dives —
    every span must reach the exporter, including the noise."""
    monkeypatch.setenv("OPENCLAW_OTEL_VERBOSE", "true")
    downstream = MagicMock()
    f = tracing.NoiseSpanFilter(downstream)

    for name in ("agent_speaking", "agent_session", "on_enter"):
        f.on_end(_fake_span(name))

    assert downstream.on_end.call_count == 3


def test_noise_filter_forwards_lifecycle_methods() -> None:
    """``on_start``, ``shutdown``, and ``force_flush`` are SDK contracts —
    delegate everything to the downstream processor."""
    downstream = MagicMock()
    downstream.force_flush.return_value = True
    f = tracing.NoiseSpanFilter(downstream)
    span = cast(Span, SimpleNamespace(name="agent_session"))
    parent_ctx = object()

    f.on_start(span, parent_ctx)
    f.shutdown()
    assert f.force_flush(timeout_millis=1234) is True

    downstream.on_start.assert_called_once_with(span, parent_ctx)
    downstream.shutdown.assert_called_once()
    downstream.force_flush.assert_called_once_with(1234)


# ---------------------------------------------------------------------------
# attach_current_span_metadata
# ---------------------------------------------------------------------------


def test_attach_writes_session_and_enduser_semconv_keys() -> None:
    """``session.id`` and ``enduser.id`` are the OTel SemConv attributes
    operators filter Traces by — they must land on the active span."""
    span = MagicMock()
    span.is_recording.return_value = True

    with patch("opentelemetry.trace.get_current_span", return_value=span):
        tracing.attach_current_span_metadata(
            session_id="CA12345",
            enduser_id="+15551234567",
            dialed_number="+15559876543",
            caller_source="twilio",
            lk_job_id="job-7",
        )

    span.set_attribute.assert_any_call("session.id", "CA12345")
    span.set_attribute.assert_any_call("enduser.id", "+15551234567")
    span.set_attribute.assert_any_call("openclaw.dialed_number", "+15559876543")
    span.set_attribute.assert_any_call("openclaw.caller_source", "twilio")
    span.set_attribute.assert_any_call("openclaw.lk_job_id", "job-7")


def test_attach_skips_falsey_values() -> None:
    """Empty / None values must not pollute the span with empty strings."""
    span = MagicMock()
    span.is_recording.return_value = True

    with patch("opentelemetry.trace.get_current_span", return_value=span):
        tracing.attach_current_span_metadata(
            session_id="CA12345",
            enduser_id=None,
            dialed_number="",
            caller_source=None,
            lk_job_id=None,
        )

    span.set_attribute.assert_called_once_with("session.id", "CA12345")


def test_attach_is_noop_when_span_not_recording() -> None:
    """Outside the LiveKit session context (or when sampling drops the
    span), :func:`attach_current_span_metadata` must do nothing."""
    span = MagicMock()
    span.is_recording.return_value = False

    with patch("opentelemetry.trace.get_current_span", return_value=span):
        tracing.attach_current_span_metadata(session_id="CA1", enduser_id="+1")

    span.set_attribute.assert_not_called()


# ---------------------------------------------------------------------------
# flush + shutdown wiring
# ---------------------------------------------------------------------------


def test_force_flush_uses_configured_provider() -> None:
    provider = MagicMock()
    provider.force_flush.return_value = True
    tracing._provider = provider

    assert tracing.force_flush_otel_tracing(timeout_millis=1234) is True

    provider.force_flush.assert_called_once_with(1234)


def test_wire_shutdown_flush_registers_job_callback() -> None:
    ctx = MagicMock()

    tracing.wire_otel_shutdown_flush(ctx, timeout_millis=2345)

    ctx.add_shutdown_callback.assert_called_once()
    callback = ctx.add_shutdown_callback.call_args.args[0]
    with patch.object(tracing, "force_flush_otel_tracing", return_value=True) as mock_flush:
        callback()
    mock_flush.assert_called_once_with(2345)


def teardown_module(module) -> None:  # noqa: ARG001
    """Don't leak module state into other test modules — they may import
    ``sdk.env`` (which calls setup) and skip wiring if we leave it set."""
    _reset_module_state()
    for k in (
        "OPENCLAW_OTEL_ENABLED",
        "OPENCLAW_OTLP_ENDPOINT",
        "OPENCLAW_OTEL_VERBOSE",
        "OPENCLAW_AGENT_NAME",
        "OPENCLAW_DEPLOYMENT_ENVIRONMENT",
        "OPENCLAW_SERVICE_VERSION",
    ):
        os.environ.pop(k, None)
