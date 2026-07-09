"""The voice model's tool surface, pinned.

`openclaw_delegate` / `sessions_send` / `sessions_spawn` / `schedule_callback`
were removed with the OpenClaw gateway. This test fails if any of them come
back by accident — a prompt that promises a tool the runtime doesn't register
makes the model fabricate.
"""

from conftest import ComposedAgent
from livekit.agents import FunctionTool

EXPECTED_TOOLS = {
    "get_current_time",
    "get_weather",
    "musubi_get",
    "musubi_recent",
    "musubi_remember",
    "musubi_search",
    "musubi_think",
}

REMOVED_TOOLS = [
    "openclaw_delegate",
    "sessions_send",
    "sessions_spawn",
    "schedule_callback",
]


def _tool_names(agent) -> set[str]:
    return {
        name
        for name in dir(agent)
        if isinstance(getattr(type(agent), name, None), FunctionTool)
        or isinstance(getattr(agent, name, None), FunctionTool)
    }


def test_composed_agent_exposes_expected_tools():
    agent = ComposedAgent()
    assert EXPECTED_TOOLS <= set(dir(agent))


def test_removed_tools_are_not_attributes():
    agent = ComposedAgent()
    for name in REMOVED_TOOLS:
        assert getattr(agent, name, None) is None, f"{name} is back on the agent"


def test_sessions_mixin_is_gone():
    import tools

    assert not hasattr(tools, "SessionsToolsMixin")
