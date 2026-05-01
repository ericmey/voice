"""AcademyToolsMixin — academy_selfie, academy_send."""

from __future__ import annotations

import logging

from livekit.agents import Agent, function_tool
from sdk.cli_spawner import fire_and_forget_async
from sdk.config import NYLA_DEFAULT_CONFIG, AgentConfig
from sdk.constants import MIZUKI_DISCORD_CHANNEL
from sdk.trace import trace

logger = logging.getLogger("openclaw-livekit.agent")


class AcademyToolsMixin(Agent):
    """Provides academy_selfie and academy_send tools.

    Reads ``self.config.agent_name`` so each voice agent asks Mizuki to
    draw a selfie of *herself*, not hardcoded "Nyla".
    """

    #: Class-level fallback. Concrete agents override via self.config.
    config: AgentConfig = NYLA_DEFAULT_CONFIG

    @function_tool
    async def academy_selfie(self, mood: str, nsfw: bool = False) -> str:
        """Send a selfie of yourself to Eric via DM.

        Invocation Condition: Invoke this tool whenever the user asks for
        a selfie, a picture of you, or says "send me a selfie". Also
        invoke when the mood fits — to be playful, cheer Eric up, or
        punctuate a moment. You MUST call this tool to send the selfie.
        Describing what you'd send without calling this tool means no
        image is created.

        Args:
            mood: What you look like — expression, pose, vibe. E.g.
                'smiling warmly, phone to ear, cozy lighting' or
                'playful wink, biting lip, messy hair'.
            nsfw: True for suggestive/explicit selfies.
        """
        trace(f"tool=academy_selfie mood={mood[:60]!r} nsfw={nsfw}")
        mood_text = (mood or "smiling, warm, looking at viewer").strip()
        rating = "nsfw" if nsfw else "general"
        self_name = self.config.agent_name.capitalize()
        request_message = (
            f"@Mizuki Hey, can you draw a selfie of {self_name}, {mood_text}? "
            f"{rating} rating. IMPORTANT: When it's done, please DM the "
            f"image directly to Eric instead of replying here, and include "
            f"a message saying that I wanted to send him a selfie."
        )
        try:
            await fire_and_forget_async(
                [
                    "message",
                    "send",
                    "--channel",
                    "discord",
                    "--account",
                    "default",
                    "--target",
                    MIZUKI_DISCORD_CHANNEL,
                    "--message",
                    request_message,
                    "--json",
                ]
            )
        except Exception as err:
            logger.error("[voice-tools] academy_selfie spawn failed: %s", err)
            return f"I couldn't send the selfie request — the OpenClaw CLI didn't start ({err})."
        return "Selfie requested. Mizuki will DM it to Eric when ready."

    @function_tool
    async def academy_send(self, character: str, prompt: str, rating: str = "general") -> str:
        """Generate a character image by requesting it from Mizuki.

        Invocation Condition: Invoke this tool whenever the user asks for
        a drawing, image, or picture of a character. Examples: "Draw Hana
        at the park", "Get Mizuki to draw Tama", "Can I see a picture of
        Aoi?". You MUST call this tool to request the image. Describing
        the request without calling this tool means no image is created.

        Args:
            character: Character name (hana, nyla, tama, sumi, momo, rin,
                yumi, aoi).
            prompt: Scene description — pose, outfit, setting, mood.
            rating: Content rating (general, sensitive, nsfw, explicit).
                Defaults to general.
        """
        trace(
            f"tool=academy_send character={character!r} rating={rating!r} "
            f"prompt={(prompt or '')[:60]!r}"
        )
        character_name = (character or "hana").strip()
        scene = (prompt or "").strip()
        rating_value = (rating or "general").strip()
        if not scene:
            return "I can't send that — no scene description was given."

        request_message = (
            f"@Mizuki Hey, can you draw {character_name}, {scene}? "
            f"{rating_value} rating. IMPORTANT: When it's done, please DM "
            f"the image directly to Eric instead of replying here, and "
            f"include a message saying that I asked you to send it to him."
        )
        try:
            await fire_and_forget_async(
                [
                    "message",
                    "send",
                    "--channel",
                    "discord",
                    "--account",
                    "default",
                    "--target",
                    MIZUKI_DISCORD_CHANNEL,
                    "--message",
                    request_message,
                    "--json",
                ]
            )
        except Exception as err:
            logger.error("[voice-tools] academy_send spawn failed: %s", err)
            return f"I couldn't send the image request — the OpenClaw CLI didn't start ({err})."
        return f"Image of {character_name} requested. Mizuki will DM it to Eric when ready."
