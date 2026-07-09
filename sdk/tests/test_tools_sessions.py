"""Tests for SessionsToolsMixin — callback guardrails.

OpenClaw delegation was removed 2026-07-08 (standalone voice agents on
mizuki, no legacy gateway). The delegation tests that lived here went
with it; what remains is the disabled schedule_callback surface.
"""

from tools.sessions import SessionsToolsMixin


def test_sessions_mixin_has_schedule_callback():
    assert hasattr(SessionsToolsMixin, "schedule_callback")
    assert callable(SessionsToolsMixin.schedule_callback)


def test_sessions_mixin_has_no_openclaw_delegate():
    """Delegation is gone — the tool and its helpers must not be present."""
    for removed in (
        "openclaw_delegate",
        "_delegate_to_openclaw",
        "sessions_send",
        "sessions_spawn",
        "_delivery_target",
        "_reject_delegation_target",
    ):
        assert not hasattr(SessionsToolsMixin, removed), f"{removed} should be removed"


def test_composed_agent_has_sessions_tools(agent):
    """The callback surface is discoverable on a composed agent instance."""
    assert hasattr(agent, "schedule_callback")


def test_schedule_callback_reads_caller_from(agent):
    """schedule_callback accesses _caller_from set by concrete class."""
    assert agent._caller_from == "+15551234567"
