"""Shared setup for the Yua voice agent.

Thin wrapper around :mod:`tools.base_agent` so Yua-specific config
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
    "YUA_CONFIG",
    "YUA_VOICE",
    "YuaAgent",
    "build_model",
    "build_tools",
    "load_env_once",
    "load_persona",
]

_PROMPTS_DIR = Path(__file__).resolve().parent.parent / "prompts"
YUA_VOICE = "Leda"

#: Yua's operational identity. Shares Nyla's Discord room for now
#: until Eric carves out a dedicated room. Memory goes to the yua-voice
#: bucket so phone calls stay separable from her other streams.
YUA_CONFIG = AgentConfig(
    agent_name="yua",
    memory_agent_tag="yua-voice",
    discord_room=NYLA_DISCORD_ROOM,
    # Canonical Musubi under agent-as-tenant (ADR 0030): Yua writes to
    # ``yua/voice/*`` and surveys the same household list as Nyla.
    musubi_v2_namespace="yua/voice",
    musubi_v2_presence="yua/voice",
    household_presences=(
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
    ),
)


class YuaAgent(HouseholdToolsMixin, BaseRealtimeAgent):
    """Yua with the core + Musubi tool set and the household survey."""

    config = YUA_CONFIG


def build_model():
    """Gemini 2.5 Flash Native Audio with Yua's selected voice."""
    return build_realtime_model(voice=YUA_VOICE)


build_tools = build_common_tools


def load_persona() -> str:
    """Load Yua's persona from prompts/system.md."""
    return _load_persona(_PROMPTS_DIR)
