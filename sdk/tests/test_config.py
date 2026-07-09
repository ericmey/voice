"""Tests for AgentConfig + NYLA_DEFAULT_CONFIG."""

import pytest
from sdk.config import NYLA_DEFAULT_CONFIG, AgentConfig


def test_agent_config_is_frozen():
    """Immutability — a config once built can't be silently mutated."""
    cfg = AgentConfig(
        agent_name="x",
        memory_agent_tag="x-voice",
    )
    with pytest.raises(AttributeError):
        cfg.agent_name = "y"  # type: ignore[misc]


def test_nyla_default_config_values():
    """The SDK-level default tags everything as Nyla."""
    assert NYLA_DEFAULT_CONFIG.agent_name == "nyla"
    assert NYLA_DEFAULT_CONFIG.memory_agent_tag == "nyla-voice"


def test_agent_config_has_no_delegation_allowlist():
    """Delegation went with the gateway; the field should not come back quietly."""
    cfg = AgentConfig(
        agent_name="aoi",
        memory_agent_tag="aoi-voice",
    )
    assert not hasattr(cfg, "allowed_delegation_targets")
