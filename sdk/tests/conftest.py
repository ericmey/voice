"""Shared fixtures for SDK tests."""

import pytest
from livekit.agents import Agent
from tools.core import CoreToolsMixin
from tools.memory import MemoryToolsMixin
from tools.sessions import SessionsToolsMixin


class ComposedAgent(
    CoreToolsMixin,
    MemoryToolsMixin,
    SessionsToolsMixin,
    Agent,
):
    """Test agent with all mixins composed."""

    def __init__(self) -> None:
        super().__init__(instructions="test persona")
        self._caller_from: str | None = "+15551234567"


@pytest.fixture
def agent() -> ComposedAgent:
    return ComposedAgent()
