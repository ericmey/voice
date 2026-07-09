"""Shared setup for the Aoi voice agent.

Thin wrapper around :mod:`tools.base_agent` so Aoi-specific config
lives here while shared scaffolding lives in one place.
"""

from __future__ import annotations

from pathlib import Path

from sdk.config import AgentConfig
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
    "AOI_CONFIG",
    "AoiAgent",
    "build_model",
    "build_tools",
    "load_env_once",
    "load_persona",
]

_PROMPTS_DIR = Path(__file__).resolve().parent.parent / "prompts"

#: Aoi's operational identity. Memory goes to the aoi-voice bucket so her
#: stored context is separable from Nyla's.
AOI_CONFIG = AgentConfig(
    agent_name="aoi",
    memory_agent_tag="aoi-voice",
    # Canonical Musubi under agent-as-tenant (ADR 0030): Aoi writes to
    # ``aoi/voice/*`` and surveys the same household list as Nyla.
    musubi_v2_namespace="aoi/voice",
    musubi_v2_presence="aoi/voice",
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


class AoiAgent(HouseholdToolsMixin, BaseRealtimeAgent):
    """Aoi with the core + Musubi tool set and the household survey."""

    config = AOI_CONFIG


def build_model():
    """Gemini 2.5 Flash Native Audio, Kore voice."""
    return build_realtime_model(voice="Kore")


build_tools = build_common_tools


def load_persona() -> str:
    """Load Aoi's persona from prompts/system.md."""
    return _load_persona(_PROMPTS_DIR)
