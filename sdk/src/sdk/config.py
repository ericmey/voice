"""AgentConfig — operational identity for a voice agent.

One source of truth for the behavioral/infra fields that multiple tools
and telemetry paths need. Concrete agents build an AgentConfig and
assign it to ``self.config``; the mixin stack reads from ``self.config``
instead of module-level constants. Centralizing this here is what keeps
Aoi's memories from being attributed to Nyla and keeps Aoi's delegated
work from landing in Nyla's Discord room.
"""

from __future__ import annotations

from dataclasses import dataclass

from .constants import NYLA_DISCORD_ROOM


@dataclass(frozen=True)
class AgentConfig:
    """Per-agent behavioral/infra identity.

    Attributes:
        agent_name: Short canonical id ("nyla", "aoi", "party"). Used in
            telemetry, the cron callback ``--agent`` slot, and in the
            agent's own self-reference (e.g. "selfie of Nyla").
        memory_agent_tag: Value written into stored Musubi memories'
            ``payload.agent`` field. Separates voice identities so the
            household can filter memories by speaker.
        discord_room: Discord target (``channel:<id>`` or ``user:<id>``)
            where delegated work results should post when the voice agent
            delegates with ``deliver_to="room"``. Typically the agent's
            own Discord channel.
        allowed_delegation_targets: Optional whitelist of agent ids this
            voice agent may delegate to via ``openclaw_delegate``.
            ``None`` means no restriction (household
            router behavior — Nyla's default). A frozenset means any
            ``agent_id`` outside the set is rejected with an error
            message rather than firing.
        musubi_v2_namespace: Two-segment ``<agent>/<channel>`` prefix
            (Musubi ADR 0030 agent-as-tenant form) used by the canonical
            Musubi tools (``musubi_search`` / ``musubi_recent`` /
            ``musubi_remember`` / ``musubi_think`` / ``musubi_get``) in
            ``tools/memory.py``. The mixin appends the plane segment at
            call time: ``<prefix>/episodic`` for remember/recent,
            ``<prefix>/thought`` for think; ``musubi_search`` widens
            to ``<tenant>/*/episodic`` per ADR 0031 for cross-channel
            recall. Example: ``aoi/voice``. ``None`` means the mixin
            is not meaningfully configured for this agent — tools
            degrade gracefully. Left ``None`` on every existing agent
            so the migration is
            deliberate.
        musubi_v2_presence: Presence identifier used as
            ``from_presence`` for `musubi_think` thought sends. Shape:
            ``<owner>/<agent>`` (e.g. ``eric/aoi``). Defaults to
            ``eric/<agent_name>`` at call time when ``None``.
        household_presences: Presences this agent may survey via
            ``household_status`` (``HouseholdToolsMixin``). Each entry
            is a 2-segment presence like ``eric/nyla``. Empty tuple
            means the agent has no household-wide visibility — the
            mixin should not be mixed in for that agent. Nyla and Aoi
            get the full household list; party/voice personas that
            mirror another agent get an empty tuple by default.
    """

    agent_name: str
    memory_agent_tag: str
    discord_room: str
    allowed_delegation_targets: frozenset[str] | None = None
    musubi_v2_namespace: str | None = None
    musubi_v2_presence: str | None = None
    household_presences: tuple[str, ...] = ()


# Default config preserves the pre-AgentConfig behavior: tag everything
# as Nyla-voice, deliver room-targeted work to Nyla's channel, no
# delegation restrictions.
#
# WARNING: this is the class-level fallback on every mixin. If a new
# agent forgets to set its own ``config``, it will silently pollute
# Nyla's memory bucket and Discord room. Always set ``config`` on
# concrete agent subclasses.
NYLA_DEFAULT_CONFIG = AgentConfig(
    agent_name="nyla",
    memory_agent_tag="nyla-voice",
    discord_room=NYLA_DISCORD_ROOM,
    allowed_delegation_targets=None,
)
