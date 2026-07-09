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


def test_retired_gateway_surface_removed():
    """The gateway tool surface was deleted, not deprecated. These names must
    never reappear on the core mixin — a prompt that promises a tool the
    runtime doesn't register makes the model fabricate the result.

    (Kept deliberately: this test names the dead symbols so that a grep for
    them finds this guard rather than a resurrection.)
    """
    for name in ("openclaw_request", "openclaw_delegate"):
        assert not callable(getattr(CoreToolsMixin, name, None)), f"{name} is back"
