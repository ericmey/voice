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
from tools.core import CoreToolsMixin
from tools.memory import MusubiToolsMixin

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
    # Canonical Musubi under agent-as-tenant (ADR 0030): Aoi writes to her own
    # ``<agent>/voice/*`` namespace.
    musubi_v2_namespace="aoi/voice",
)


# Composition is EXPLICIT. The base class no longer decides which tools Aoi has.
#
# `BaseRealtimeAgent` used to bake in `CoreToolsMixin, MusubiToolsMixin`, so a subclass could
# only ADD tools, never choose a different set — and an agent who genuinely needed a
# different composition had to bypass the base entirely (which is what Sumi did, and how she
# ended up with a duplicated persona loader and her own divergent defaults).
#
# Adding a capability now means adding a mixin to THIS line. Nyla's Hermes tools, Aoi's
# Claude Code channel, Yua's Codex channel — each lands here, on the agent who has it.
class AoiAgent(
    CoreToolsMixin,
    MusubiToolsMixin,
    BaseRealtimeAgent,
):
    """Aoi — core + Musubi memory tools."""

    config = AOI_CONFIG


def build_model():
    """Gemini 2.5 Flash Native Audio, Kore voice."""
    return build_realtime_model(voice="Kore")


build_tools = build_common_tools


def load_persona() -> str:
    """Load Aoi's persona from prompts/system.md."""
    return _load_persona(_PROMPTS_DIR)
