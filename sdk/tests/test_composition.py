"""The voice model's tool surface, pinned.

`openclaw_delegate` / `sessions_send` / `sessions_spawn` / `schedule_callback`
were removed with the OpenClaw gateway. This fails if any of them come back by
accident — a prompt that promises a tool the runtime doesn't register makes the
model fabricate.
"""

import pytest

EXPECTED_TOOLS = [
    "get_current_time",
    "get_weather",
    "musubi_recent",
    "musubi_remember",
    "musubi_search",
    "musubi_think",
]

REMOVED_TOOLS = [
    # a registered tool that returned "not yet available" — the same defect
    # that took openclaw_delegate down.
    "musubi_get",
    "openclaw_delegate",
    "sessions_send",
    "sessions_spawn",
    "schedule_callback",
]


@pytest.mark.parametrize("name", EXPECTED_TOOLS)
def test_composed_agent_exposes_expected_tool(agent, name):
    assert hasattr(agent, name), f"missing tool: {name}"


@pytest.mark.parametrize("name", REMOVED_TOOLS)
def test_removed_tools_are_absent(agent, name):
    assert getattr(agent, name, None) is None, f"{name} is back on the agent"


def test_sessions_mixin_is_gone():
    import tools

    assert not hasattr(tools, "SessionsToolsMixin")
