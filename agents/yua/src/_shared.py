"""Shared setup for the Yua voice agent.

Thin wrapper around :mod:`tools.base_agent` so Yua-specific config
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
from tools.core import CoreToolsMixin
from tools.memory import MusubiToolsMixin

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

#: Yua's operational identity. Memory goes to the yua-voice bucket so phone
#: calls stay separable from her other streams.
YUA_CONFIG = AgentConfig(
    agent_name="yua",
    memory_agent_tag="yua-voice",
    # Canonical Musubi under agent-as-tenant (ADR 0030): Yua writes to her own
    # ``<agent>/voice/*`` namespace.
    musubi_v2_namespace="yua/voice",
)


# Composition is EXPLICIT. The base class no longer decides which tools Yua has.
#
# `BaseRealtimeAgent` used to bake in `CoreToolsMixin, MusubiToolsMixin`, so a subclass could
# only ADD tools, never choose a different set — and an agent who genuinely needed a
# different composition had to bypass the base entirely (which is what Sumi did, and how she
# ended up with a duplicated persona loader and her own divergent defaults).
#
# Adding a capability now means adding a mixin to THIS line. Nyla's Hermes tools, Aoi's
# Claude Code channel, Yua's Codex channel — each lands here, on the agent who has it.
class YuaAgent(
    CoreToolsMixin,
    MusubiToolsMixin,
    BaseRealtimeAgent,
):
    """Yua — core + Musubi memory tools."""

    config = YUA_CONFIG


def build_model():
    """Gemini 2.5 Flash Native Audio with Yua's selected voice."""
    return build_realtime_model(voice=YUA_VOICE)


build_tools = build_common_tools


def load_persona() -> str:
    """Load Yua's persona from prompts/system.md."""
    return _load_persona(_PROMPTS_DIR)
