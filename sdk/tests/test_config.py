"""Tests for AgentConfig — derived identity, the fail-loud sentinel, and the
startup identity assertion."""

import pytest
from sdk.config import UNCONFIGURED_CONFIG, AgentConfig, assert_agent_identity


def test_agent_config_is_frozen():
    """Immutability — a config once built can't be silently mutated."""
    cfg = AgentConfig(agent_name="x", memory_agent_tag="x-voice")
    with pytest.raises(AttributeError):
        cfg.agent_name = "y"  # type: ignore[misc]


def test_registration_name_derives_from_agent_name():
    """registration_name is always ``phone-<agent_name>`` — derived, never
    hand-typed, so the LiveKit/telemetry spelling can't drift from the memory
    identity (the three-headed-identity bug)."""
    cfg = AgentConfig(agent_name="aoi", memory_agent_tag="aoi-voice")
    assert cfg.registration_name == "phone-aoi"


def test_unconfigured_sentinel_fails_safe():
    """The class-level default is a sentinel with no namespace, so an
    unconfigured agent degrades to 'memory unavailable' instead of silently
    becoming a real tenant (the old NYLA_DEFAULT_CONFIG footgun)."""
    assert UNCONFIGURED_CONFIG.agent_name == "__unconfigured__"
    assert UNCONFIGURED_CONFIG.musubi_v2_namespace is None


def test_agent_config_has_no_delegation_allowlist():
    """Delegation went with the gateway; the field should not come back quietly."""
    cfg = AgentConfig(agent_name="aoi", memory_agent_tag="aoi-voice")
    assert not hasattr(cfg, "allowed_delegation_targets")


def test_agent_config_has_no_presence_field():
    """musubi_v2_presence was redundant with musubi_v2_namespace and is removed."""
    cfg = AgentConfig(agent_name="aoi", memory_agent_tag="aoi-voice")
    assert not hasattr(cfg, "musubi_v2_presence")


def test_assert_agent_identity_matches(monkeypatch):
    monkeypatch.setenv("VOICE_AGENT_NAME", "aoi")
    # Must not raise when env and config agree.
    assert_agent_identity(AgentConfig(agent_name="aoi", memory_agent_tag="aoi-voice"))


def test_assert_agent_identity_mismatch_raises(monkeypatch):
    """The exact check that would have caught an agent registering under one
    name while its config claimed to be someone else."""
    monkeypatch.setenv("VOICE_AGENT_NAME", "sumi")
    with pytest.raises(RuntimeError, match="identity mismatch"):
        assert_agent_identity(AgentConfig(agent_name="nyla", memory_agent_tag="nyla-voice"))


def test_assert_agent_identity_unset_is_lenient(monkeypatch):
    """Dev/test runs outside the entrypoint (no VOICE_AGENT_NAME) skip the check."""
    monkeypatch.delenv("VOICE_AGENT_NAME", raising=False)
    assert_agent_identity(AgentConfig(agent_name="aoi", memory_agent_tag="aoi-voice"))
