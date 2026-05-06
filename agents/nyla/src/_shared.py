"""Shared setup for Nyla voice and text agents.

Everything that must be identical between phone-nyla (voice) and
phone-nyla-text (text-only): model, tools, persona, agent class.

Thin wrapper around :mod:`tools.base_agent` so Nyla-specific config
lives here while shared scaffolding lives in one place.
"""

from __future__ import annotations

from pathlib import Path

from sdk.config import AgentConfig
from sdk.constants import NYLA_DISCORD_ROOM
from tools.base_agent import (
    BaseRealtimeAgent,
    build_common_tools,
    build_realtime_model,
    load_env_once,
)
from tools.base_agent import (
    load_persona as _load_persona,
)
from tools.household import HouseholdToolsMixin

__all__ = [
    "NYLA_CONFIG",
    "NYLA_VOICE",
    "NylaAgent",
    "build_model",
    "build_tools",
    "load_env_once",
    "load_persona",
]

_PROMPTS_DIR = Path(__file__).resolve().parent.parent / "prompts"
NYLA_VOICE = "Aoede"

#: Nyla's operational identity. Household router — no delegation
#: restrictions, delegated work posts to her own Discord room.
#:
#: Canonical Musubi fields (musubi_v2_*, household_presences) wire the
#: v2 client + household-status tool per ADR 0030 (agent-as-tenant).
#:
#: - musubi_v2_namespace / _presence: 2-seg ``<agent>/<channel>``.
#:   Memory writes land at ``nyla/voice/episodic``; thought sends
#:   carry ``from_presence = nyla/voice``.
#: - household_presences: every agent's voice presence Nyla may
#:   survey when asked "what's been going on". Token scope grants
#:   ``*/*/*:r`` to resolve all of them — cross-agent write still 403s.
HOUSEHOLD_VOICE_PRESENCES: tuple[str, ...] = (
    # nyla machine
    "nyla/voice",
    "aoi/voice",
    "hana/voice",
    "rin/voice",
    "sumi/voice",
    "tama/voice",
    "yumi/voice",
    # hana machine
    "mizuki/voice",
    "shiori/voice",
    "reika/voice",
    "yua/voice",
    "nana/voice",
)

NYLA_CONFIG = AgentConfig(
    agent_name="nyla",
    memory_agent_tag="nyla-voice",
    discord_room=NYLA_DISCORD_ROOM,
    allowed_delegation_targets=None,
    musubi_v2_namespace="nyla/voice",
    musubi_v2_presence="nyla/voice",
    household_presences=HOUSEHOLD_VOICE_PRESENCES,
)


class NylaAgent(HouseholdToolsMixin, BaseRealtimeAgent):
    """Nyla with all OpenClaw platform tools + household survey."""

    config = NYLA_CONFIG


def build_model():
    """Gemini 2.5 Flash Native Audio with Nyla's selected voice."""
    return build_realtime_model(voice=NYLA_VOICE)


build_tools = build_common_tools


def load_persona() -> str:
    """Load Nyla's persona from prompts/system.md."""
    return _load_persona(_PROMPTS_DIR)
