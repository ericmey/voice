"""Tests for the OTel tracing setup module.

Covers the gating contract: ``setup_otel_tracing`` must be a hard no-op
when neither ``OPENCLAW_OTEL_ENABLED`` nor the legacy
``LANGSMITH_TRACING`` flag is set, so unit tests, CI, and local dev
stay hermetic. When enabled, it must wire LiveKit's tracer provider
with the configured exporters (default ``otlp`` after the SigNoz-primary
refactor on 2026-05-01).
"""

from __future__ import annotations

import os
from unittest.mock import MagicMock, patch

from sdk import tracing


def _reset_module_state() -> None:
    """Tracing setup is module-level idempotent — reset between tests."""
    tracing._initialized = False
    tracing._provider = None


def test_setup_is_noop_when_env_var_unset(monkeypatch) -> None:
    """No env var = no tracer provider mutation, no exceptions."""
    monkeypatch.delenv("OPENCLAW_OTEL_ENABLED", raising=False)
    monkeypatch.delenv("LANGSMITH_TRACING", raising=False)
    _reset_module_state()

    with patch("livekit.agents.telemetry.set_tracer_provider") as mock_set:
        tracing.setup_otel_tracing()

    mock_set.assert_not_called()
    assert tracing._initialized is False


def test_setup_is_noop_when_env_var_false(monkeypatch) -> None:
    """Explicitly disabled is the same as unset — provider stays untouched."""
    monkeypatch.setenv("OPENCLAW_OTEL_ENABLED", "false")
    monkeypatch.delenv("LANGSMITH_TRACING", raising=False)
    _reset_module_state()

    with patch("livekit.agents.telemetry.set_tracer_provider") as mock_set:
        tracing.setup_otel_tracing()

    mock_set.assert_not_called()


def test_setup_is_noop_when_otlp_endpoint_missing(monkeypatch) -> None:
    """Tracing requested but no OTel endpoint configured → degrade quietly
    with a warning, not a crash. Operator forgot a half-step in the env
    file; production must keep serving calls regardless."""
    monkeypatch.setenv("OPENCLAW_OTEL_ENABLED", "true")
    monkeypatch.delenv("OPENCLAW_OTLP_ENDPOINT", raising=False)
    monkeypatch.delenv("OTEL_EXPORTER_OTLP_ENDPOINT", raising=False)
    monkeypatch.delenv("OTEL_EXPORTER_OTLP_HEADERS", raising=False)
    _reset_module_state()

    with patch("livekit.agents.telemetry.set_tracer_provider") as mock_set:
        tracing.setup_otel_tracing()

    mock_set.assert_not_called()


def test_legacy_langsmith_tracing_flag_still_enables_setup(monkeypatch) -> None:
    """The old LANGSMITH_TRACING flag still flips on the OTel pipeline so
    operators upgrading from a pre-rename branch don't lose telemetry."""
    monkeypatch.delenv("OPENCLAW_OTEL_ENABLED", raising=False)
    monkeypatch.setenv("LANGSMITH_TRACING", "true")
    monkeypatch.setenv("OPENCLAW_OTLP_ENDPOINT", "http://localhost:4318/v1/traces")
    _reset_module_state()

    with (
        patch("livekit.agents.telemetry.set_tracer_provider") as mock_set,
        patch("opentelemetry.trace.set_tracer_provider"),
    ):
        tracing.setup_otel_tracing()

    mock_set.assert_called_once()


def test_setup_wires_provider_when_fully_configured(monkeypatch) -> None:
    """Happy path on the SigNoz-primary defaults — enabled flag plus an
    OTLP endpoint must wire LiveKit's tracer provider. Asserts the
    integration point LiveKit relies on (``set_tracer_provider``) is
    actually called."""
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


def test_setup_overrides_langsmith_project_header_from_agent_env(monkeypatch) -> None:
    """When the optional ``langsmith`` exporter is in play, launchd
    renders LANGSMITH_PROJECT per agent and setup must rewrite the
    OTLP project header before the LangSmith exporter is constructed."""
    monkeypatch.setenv("OPENCLAW_OTEL_ENABLED", "true")
    monkeypatch.setenv("LANGSMITH_PROJECT", "Nyla")
    monkeypatch.setenv("OTEL_EXPORTER_OTLP_ENDPOINT", "https://api.smith.langchain.com/otel")
    monkeypatch.setenv(
        "OTEL_EXPORTER_OTLP_HEADERS", "x-api-key=test-key,Langsmith-Project=Harem World"
    )
    _reset_module_state()

    with (
        patch("livekit.agents.telemetry.set_tracer_provider"),
        patch("opentelemetry.trace.set_tracer_provider"),
    ):
        tracing.setup_otel_tracing()

    assert os.environ["OTEL_EXPORTER_OTLP_HEADERS"] == "x-api-key=test-key,Langsmith-Project=Nyla"


def test_agent_name_defaults_langsmith_project(monkeypatch) -> None:
    """``_agent_langsmith_project`` is a LangSmith-specific helper that
    only runs when the langsmith exporter is configured. It picks the
    per-agent project name from OPENCLAW_AGENT_NAME when LANGSMITH_PROJECT
    isn't set explicitly."""
    monkeypatch.delenv("LANGSMITH_PROJECT", raising=False)
    monkeypatch.setenv("OPENCLAW_AGENT_NAME", "aoi")

    assert tracing._agent_langsmith_project() == "Aoi"


def test_setup_is_idempotent(monkeypatch) -> None:
    """Multiple calls (re-imports, accidental double-init) must not register
    the processor twice — we'd duplicate every span."""
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


def test_setup_otel_tracing_idempotent_under_full_config(monkeypatch) -> None:
    """Sanity check that the canonical ``setup_otel_tracing`` entrypoint
    initializes provider state when fully configured."""
    monkeypatch.setenv("OPENCLAW_OTEL_ENABLED", "true")
    monkeypatch.setenv("OPENCLAW_OTLP_ENDPOINT", "http://localhost:4318/v1/traces")
    _reset_module_state()

    with (
        patch("livekit.agents.telemetry.set_tracer_provider"),
        patch("opentelemetry.trace.set_tracer_provider"),
    ):
        tracing.setup_otel_tracing()

    assert tracing._initialized is True
    assert tracing._provider is not None


def test_exporters_default_to_otlp(monkeypatch) -> None:
    """Default exporter is now ``otlp`` (SigNoz-primary, 2026-05-01).
    Operators who want LangSmith opt in via OPENCLAW_OTEL_EXPORTERS."""
    monkeypatch.delenv("OPENCLAW_OTEL_EXPORTERS", raising=False)
    monkeypatch.setenv("OPENCLAW_OTEL_ENABLED", "true")
    monkeypatch.setenv("OPENCLAW_OTLP_ENDPOINT", "http://localhost:4318/v1/traces")
    _reset_module_state()

    captured: list[str] = []

    def fake_add_exporter(provider, name):
        captured.append(name)
        return True

    with (
        patch("livekit.agents.telemetry.set_tracer_provider"),
        patch("opentelemetry.trace.set_tracer_provider"),
        patch.object(tracing, "_add_exporter", side_effect=fake_add_exporter),
    ):
        tracing.setup_otel_tracing()

    assert captured == ["otlp"]


def test_multiple_exporters_can_be_configured(monkeypatch) -> None:
    monkeypatch.setenv("OPENCLAW_OTEL_ENABLED", "true")
    monkeypatch.setenv("OPENCLAW_OTEL_EXPORTERS", "langsmith,console,otlp")
    monkeypatch.setenv("OTEL_EXPORTER_OTLP_ENDPOINT", "https://api.smith.langchain.com/otel")
    monkeypatch.setenv("OTEL_EXPORTER_OTLP_HEADERS", "x-api-key=k,Langsmith-Project=p")
    monkeypatch.setenv("OPENCLAW_OTLP_ENDPOINT", "http://localhost:4318/v1/traces")
    _reset_module_state()

    captured: list[str] = []

    def fake_add_exporter(provider, name):
        captured.append(name)
        return True

    with (
        patch("livekit.agents.telemetry.set_tracer_provider"),
        patch("opentelemetry.trace.set_tracer_provider"),
        patch.object(tracing, "_add_exporter", side_effect=fake_add_exporter),
    ):
        tracing.setup_otel_tracing()

    assert captured == ["langsmith", "console", "otlp"]


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
    assert attrs["openclaw.agent"] == "aoi"
    assert "service.instance.id" in attrs


def teardown_module(module) -> None:  # noqa: ARG001
    """Don't leave the module flagged as initialised — other test modules
    that import sdk.env (which calls setup) would skip their own wiring."""
    _reset_module_state()
    # Also clear env vars we may have set.
    for k in (
        "LANGSMITH_TRACING",
        "LANGSMITH_PROJECT",
        "OPENCLAW_AGENT_NAME",
        "OTEL_EXPORTER_OTLP_ENDPOINT",
        "OTEL_EXPORTER_OTLP_HEADERS",
        "OPENCLAW_OTEL_ENABLED",
        "OPENCLAW_OTEL_EXPORTERS",
        "OPENCLAW_OTLP_ENDPOINT",
        "OPENCLAW_DEPLOYMENT_ENVIRONMENT",
        "OPENCLAW_SERVICE_VERSION",
    ):
        os.environ.pop(k, None)
