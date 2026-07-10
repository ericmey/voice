"""Base realtime agent — shared scaffolding for voice agents.

Extracted from the near-identical _shared.py files in nyla/ and aoi/
so bug fixes and tuning changes propagate to both agents automatically.
"""

from __future__ import annotations

import logging
from pathlib import Path

from google.genai import types as genai_types
from livekit.agents import Agent
from livekit.agents.beta import EndCallTool
from livekit.plugins import google as google_plugin
from livekit.plugins.google.tools import GoogleSearch
from sdk.config import UNCONFIGURED_CONFIG, AgentConfig
from sdk.env import load_env

from tools.core import CoreToolsMixin
from tools.memory import MusubiToolsMixin

logger = logging.getLogger("voice.agent")

# --- env -----------------------------------------------------------------
_env_loaded = False


def load_env_once() -> None:
    global _env_loaded
    if not _env_loaded:
        load_env()
        _env_loaded = True


# --- persona -------------------------------------------------------------
_DEFAULT_PERSONA = "You are a voice assistant on a phone call."


def load_persona(prompts_dir: Path) -> str:
    path = prompts_dir / "system.md"
    if path.exists():
        return path.read_text(encoding="utf-8").strip()
    logger.warning("persona file not found: %s", path)
    return _DEFAULT_PERSONA


# --- greeting -------------------------------------------------------------

_GREETING_BASE = (
    "Open the call like a friend would, not an assistant. Be natural, "
    "varied, sometimes playful, sometimes quick. A short 'oh hey Eric, "
    "what's up?' is fine — so is a warm comment, a tease, or just 'hey.' "
    "Vary your openers across calls; don't lock into one shape. Keep "
    "it under two sentences."
)

# fetch_recent_context returns one of these user-readable strings when memory
# is unavailable/empty. They are NOT real context, so the greeting must not
# splice them in as if they were.
_DEGRADED_CONTEXT_PREFIXES = (
    "No recent memories found.",
    "Couldn't check memory",
    "Memory lookup timed out.",
)


def build_greeting_instructions(context: str | None) -> str:
    """Assemble the on_enter greeting instructions from recent context.

    Pure function so the branching (usable context vs degraded/empty) is
    unit-testable without a live session. Shared by every BaseRealtimeAgent
    subclass. Usable context is appended as *background awareness only* — the
    opener stays a natural greeting, never a formulaic recall.
    """
    has_context = bool(context) and not any(
        context.startswith(p) for p in _DEGRADED_CONTEXT_PREFIXES
    )
    if not has_context:
        return _GREETING_BASE
    return (
        _GREETING_BASE + " The recent context below is for your awareness — only "
        "mention something from it if it's genuinely notable (high importance, or "
        "Eric has been calling a lot recently). Don't lead with a recall as a "
        f"formula.\n\nRecent context (background, not a script):\n{context}"
    )


# --- agent class ---------------------------------------------------------


class BaseRealtimeAgent(
    CoreToolsMixin,
    MusubiToolsMixin,
    Agent,
):
    """Base class for realtime Gemini-native-audio voice agents.

    Subclass this, set ``config`` to an :class:`AgentConfig`, and
    override ``build_model`` if you need a different voice or VAD tuning.
    """

    config: AgentConfig = UNCONFIGURED_CONFIG

    def __init__(
        self,
        *,
        caller_from: str | None = None,
        instructions: str = "",
        extra_tools: list | None = None,
    ) -> None:
        super().__init__(instructions=instructions, tools=extra_tools or None)
        self._caller_from: str | None = caller_from

    async def on_enter(self) -> None:
        # Prefetch recent context as background awareness — NOT as the spine of
        # the greeting. Eric's feedback (2026-04-27): formulaic callbacks to
        # recent rows read as calculated. The instruction assembly lives in the
        # pure `build_greeting_instructions` below so it's unit-testable without
        # a live session.
        try:
            context = await self.fetch_recent_context(limit=10)
        except Exception as err:
            logger.warning("on_enter: startup context fetch failed: %s", err)
            context = ""
        await self.session.generate_reply(instructions=build_greeting_instructions(context))


# --- model + tools (shared) ---------------------------------------------


GEMINI_NATIVE_AUDIO_MODEL = "gemini-2.5-flash-native-audio-preview-12-2025"


def build_realtime_model(voice: str = "Leda") -> google_plugin.realtime.RealtimeModel:
    """Gemini 2.5 Flash Native Audio — identical for voice and text.

    VAD tuning notes:
    - start=HIGH: commit to user speech faster (reduces barge-in lag).
    - end=LOW: explicit; don't end user turn eagerly on pauses.
    - prefix_padding_ms=200: quick speech-onset commit.
    - silence_duration_ms=1000: Eric can pause up to 1s mid-thought
      without Gemini ending his turn.
    """
    return google_plugin.realtime.RealtimeModel(
        model=GEMINI_NATIVE_AUDIO_MODEL,
        voice=voice,
        enable_affective_dialog=True,
        proactivity=True,
        realtime_input_config=genai_types.RealtimeInputConfig(
            automatic_activity_detection=genai_types.AutomaticActivityDetection(
                start_of_speech_sensitivity=genai_types.StartSensitivity.START_SENSITIVITY_HIGH,
                end_of_speech_sensitivity=genai_types.EndSensitivity.END_SENSITIVITY_LOW,
                prefix_padding_ms=200,
                silence_duration_ms=1000,
            ),
        ),
    )


def build_common_tools() -> list:
    """Tool set — EndCall + GoogleSearch grounding."""
    return [
        EndCallTool(
            delete_room=True,
            end_instructions="Say a brief, warm goodbye to Eric.",
        ),
        GoogleSearch(),
    ]
