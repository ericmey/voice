"""Tests for SessionsToolsMixin — OpenClaw delegation and callback guardrails."""

from sdk.config import AgentConfig
from sdk.constants import (
    ERIC_DISCORD_DM,
    NYLA_DISCORD_ROOM,
)
from tools.sessions import SessionsToolsMixin


def test_sessions_mixin_has_openclaw_delegate():
    assert hasattr(SessionsToolsMixin, "openclaw_delegate")
    assert callable(SessionsToolsMixin.openclaw_delegate)


def test_sessions_mixin_has_schedule_callback():
    assert hasattr(SessionsToolsMixin, "schedule_callback")
    assert callable(SessionsToolsMixin.schedule_callback)


def test_composed_agent_has_sessions_tools(agent):
    """Session tools are discoverable on a composed agent instance."""
    assert hasattr(agent, "openclaw_delegate")
    assert hasattr(agent, "sessions_send")
    assert hasattr(agent, "sessions_spawn")
    assert hasattr(agent, "schedule_callback")


def test_schedule_callback_reads_caller_from(agent):
    """schedule_callback accesses _caller_from set by concrete class."""
    assert agent._caller_from == "+15551234567"


def test_delivery_target_resolves_room_to_agent_config(agent):
    """``room`` delivery target comes from self.config.discord_room, not
    from a hardcoded module constant."""
    assert agent._delivery_target("room") == NYLA_DISCORD_ROOM
    assert agent._delivery_target("dm") == ERIC_DISCORD_DM
    assert agent._delivery_target("garbage") is None


def test_delivery_target_follows_overridden_config():
    """Swapping the agent's config changes where ``room`` delivery goes."""
    custom = AgentConfig(
        agent_name="custom",
        memory_agent_tag="custom-voice",
        discord_room="channel:9999999999",
    )

    class _Custom(SessionsToolsMixin):
        config = custom

        def __init__(self) -> None:  # no Agent base for this unit test
            pass

    inst = _Custom()
    assert inst._delivery_target("room") == "channel:9999999999"


def test_reject_delegation_target_is_none_when_unrestricted(agent):
    """Default config has no allowlist — all targets permitted."""
    assert agent._reject_delegation_target("yumi") is None
    assert agent._reject_delegation_target("hana") is None


def test_reject_delegation_target_rejects_outside_allowlist():
    """With an allowlist set, disallowed targets get a user-facing reject."""
    restricted = AgentConfig(
        agent_name="aoi",
        memory_agent_tag="aoi-voice",
        discord_room=NYLA_DISCORD_ROOM,
        allowed_delegation_targets=frozenset({"yumi", "rin"}),
    )

    class _Restricted(SessionsToolsMixin):
        config = restricted

        def __init__(self) -> None:
            pass

    inst = _Restricted()
    assert inst._reject_delegation_target("yumi") is None
    rejection = inst._reject_delegation_target("hana")
    assert rejection is not None
    assert "hana" in rejection
    # The rejection lists the allowed targets so the voice agent can
    # surface the alternatives out loud.
    assert "yumi" in rejection and "rin" in rejection
