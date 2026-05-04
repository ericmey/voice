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
from sdk.config import NYLA_DEFAULT_CONFIG, AgentConfig
from sdk.env import load_env

from tools.academy import AcademyToolsMixin
from tools.core import CoreToolsMixin
from tools.memory import MusubiToolsMixin
from tools.sessions import SessionsToolsMixin

logger = logging.getLogger("openclaw-livekit.agent")

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


# --- agent class ---------------------------------------------------------


class BaseRealtimeAgent(
    CoreToolsMixin,
    MusubiToolsMixin,
    SessionsToolsMixin,
    AcademyToolsMixin,
    Agent,
):
    """Base class for realtime Gemini-native-audio voice agents.

    Subclass this, set ``config`` to an :class:`AgentConfig`, and
    override ``build_model`` if you need a different voice or VAD tuning.
    """

    config: AgentConfig = NYLA_DEFAULT_CONFIG

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
        # Prefetch recent context as background awareness — NOT as the
        # spine of the greeting. Eric's feedback (2026-04-27): formulaic
        # callbacks to recent rows make her feel calculated. Default is
        # a natural, varied opener like a friend; recent context is only
        # for noticing genuinely notable things (high-importance row, or
        # call-frequency cue), not for deciding what to say.
        try:
            context = await self.fetch_recent_context(limit=10)
        except Exception as err:
            logger.warning("on_enter: startup context fetch failed: %s", err)
            context = ""

        degraded_prefixes = (
            "No recent memories found.",
            "Couldn't check memory",
            "Memory lookup timed out.",
        )
        has_context = context and not any(context.startswith(p) for p in degraded_prefixes)

        base_instructions = (
            "Open the call like a friend would, not an assistant. Be natural, "
            "varied, sometimes playful, sometimes quick. A short 'oh hey Eric, "
            "what's up?' is fine — so is a warm comment, a tease, or just 'hey.' "
            "Vary your openers across calls; don't lock into one shape. Keep "
            "it under two sentences."
        )

        if has_context:
            instructions = (
                base_instructions + " The recent context below is for your awareness — only "
                "mention something from it if it's genuinely notable (high "
                "importance, or Eric has been calling a lot recently). Don't "
                "lead with a recall as a formula.\n\n"
                f"Recent context (background, not a script):\n{context}"
            )
        else:
            instructions = base_instructions

        await self.session.generate_reply(instructions=instructions)


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
    """Tool set — EndCall + GoogleSearch."""
    return [
        EndCallTool(
            delete_room=True,
            end_instructions="Say a brief, warm goodbye to Eric.",
        ),
        GoogleSearch(),
    ]
