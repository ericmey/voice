"""Shared fixtures for SDK tests."""

import pytest
from livekit.agents import Agent
from sdk.config import AgentConfig
from tools.core import CoreToolsMixin
from tools.memory import MusubiToolsMixin


class ComposedAgent(
    CoreToolsMixin,
    MusubiToolsMixin,
    Agent,
):
    """Test agent with all mixins composed.

    Sets an explicit config (the mixin default is now the fail-loud
    ``UNCONFIGURED_CONFIG`` sentinel, which degrades memory to unavailable);
    tests that need a specific identity override ``agent.config`` directly.
    """

    config = AgentConfig(
        agent_name="nyla",
        memory_agent_tag="nyla-voice",
        musubi_v2_namespace="nyla/voice",
    )

    def __init__(self) -> None:
        super().__init__(instructions="test persona")
        self._caller_from: str | None = "+15551234567"


@pytest.fixture
def agent() -> ComposedAgent:
    return ComposedAgent()
