"""Tests for CoreToolsMixin — get_current_time, get_weather."""

from tools.core import CoreToolsMixin


def test_core_mixin_has_get_current_time():
    assert hasattr(CoreToolsMixin, "get_current_time")
    assert callable(CoreToolsMixin.get_current_time)


def test_core_mixin_has_get_weather():
    assert hasattr(CoreToolsMixin, "get_weather")
    assert callable(CoreToolsMixin.get_weather)


def test_composed_agent_has_core_tools(agent):
    """Core tools are discoverable on a composed agent instance."""
    assert hasattr(agent, "get_current_time")
    assert hasattr(agent, "get_weather")
