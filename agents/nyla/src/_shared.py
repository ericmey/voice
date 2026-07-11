"""Nyla's setup — model, tools, persona, agent class.

This used to say it held "everything that must be identical between phone-nyla (voice) and
phone-nyla-text (text-only)". There is no text-only agent: `agent_text.py` was deleted and
no `phone-nyla-text` dispatch rule exists. The file survives because it is where Nyla's
config, voice and composition live — not because it is shared with a twin who does not exist.

Thin wrapper around :mod:`tools.base_agent` so Nyla-specific config
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

#: Nyla's operational identity.
#:
#: musubi_v2_namespace / _presence: 2-seg ``<agent>/<channel>``. Memory writes
#: land at ``nyla/voice/episodic``; thought sends carry
#: ``from_presence = nyla/voice``.
NYLA_CONFIG = AgentConfig(
    agent_name="nyla",
    memory_agent_tag="nyla-voice",
    musubi_v2_namespace="nyla/voice",
)


# Composition is EXPLICIT. The base class no longer decides which tools Nyla has.
#
# `BaseRealtimeAgent` used to bake in `CoreToolsMixin, MusubiToolsMixin`, so a subclass could
# only ADD tools, never choose a different set — and an agent who genuinely needed a
# different composition had to bypass the base entirely (which is what Sumi did, and how she
# ended up with a duplicated persona loader and her own divergent defaults).
#
# Adding a capability now means adding a mixin to THIS line. Nyla's Hermes tools, Aoi's
# Claude Code channel, Yua's Codex channel — each lands here, on the agent who has it.
class NylaAgent(
    CoreToolsMixin,
    MusubiToolsMixin,
    BaseRealtimeAgent,
):
    """Nyla — core + Musubi memory tools."""

    config = NYLA_CONFIG


def build_model():
    """Gemini 2.5 Flash Native Audio with Nyla's selected voice."""
    return build_realtime_model(voice=NYLA_VOICE)


build_tools = build_common_tools


def load_persona() -> str:
    """Load Nyla's persona from prompts/system.md."""
    return _load_persona(_PROMPTS_DIR)
