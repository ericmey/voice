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
]

REMOVED_TOOLS = [
    # a registered tool that returned "not yet available" — the same defect
    # that took openclaw_delegate down.
    "musubi_get",
    # presence-to-presence send, un-exposed 2026-07-10: personas forbid saying
    # "I passed it along," and the thought plane it wrote to isn't consumed by
    # the live webbing. ``think_impl`` is retained; the tool is not registered.
    "musubi_think",
    # the house concept is retired; each agent speaks for herself
    "household_status",
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
