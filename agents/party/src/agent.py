"""Party voice agent — chained STT/LLM/TTS, the Harem World line.

Registers as "phone-party" with LiveKit. Uses separate components:
  - STT: OpenAI Whisper-1 (non-streaming, needs Silero VAD)
  - VAD: Silero (segments caller audio into utterances for Whisper)
  - LLM: Gemini 3.1 Flash-Lite Preview (text model, not multimodal)
  - TTS: ElevenLabs Flash v2.5

Inherits the full OpenClaw platform tool set (Core, Memory, Sessions,
Academy). memory_agent_tag defaults to ``"nyla-voice"`` because the
Harem World line is Nyla-on-chained-pipeline — same person, different
voice engine. Override when/if Party gets its own identity.

Greeting uses session.say() — Gemini text LLM rejects generate_reply()
at session start (sends tools without a preceding user turn).
"""

from __future__ import annotations

import logging
from pathlib import Path

from livekit.agents import Agent, AgentSession, JobContext, cli
from livekit.agents.beta import EndCallTool
from livekit.agents.worker import AgentServer
from livekit.plugins import elevenlabs as elevenlabs_plugin
from livekit.plugins import google as google_plugin
from livekit.plugins import openai as openai_plugin
from livekit.plugins import silero as silero_plugin
from sdk.audio_recording import (
    annotate_call_audio_recording,
    start_call_audio_recording,
    wire_call_audio_attachment,
)
from sdk.config import AgentConfig
from sdk.constants import NYLA_DISCORD_ROOM
from sdk.env import load_env
from sdk.postcall import wire_postcall_review
from sdk.postcall_memory import wire_postcall_memory
from sdk.telemetry import wire_telemetry_capture
from sdk.telephony import resolve_caller
from sdk.trace import trace
from sdk.tracing import attach_current_span_metadata, wire_otel_shutdown_flush
from sdk.transcript import wire_transcript_logging
from tools.academy import AcademyToolsMixin
from tools.core import CoreToolsMixin
from tools.memory import MusubiToolsMixin
from tools.sessions import SessionsToolsMixin

# --- env ---------------------------------------------------------------
load_env()

logger = logging.getLogger("openclaw-livekit.agent")

# ElevenLabs voice ID (Harem World default — Nyla's voice for now).
_ELEVENLABS_VOICE_ID = "AEW6JTgnyoPaoB9zlK3S"
_ELEVENLABS_MODEL = (
    "eleven_flash_v2_5"  # streaming-compatible; eleven_v3 doesn't support multi-stream WS
)
_CHAINED_LLM_MODEL = "gemini-3.1-flash-lite-preview"

# --- persona -----------------------------------------------------------
_PROMPTS_DIR = Path(__file__).resolve().parent.parent / "prompts"
_DEFAULT_PERSONA = "You are the Harem World host on a phone call with Eric."


def _load_persona() -> str:
    path = _PROMPTS_DIR / "system.md"
    if path.exists():
        return path.read_text(encoding="utf-8").strip()
    logger.warning("persona file not found: %s", path)
    return _DEFAULT_PERSONA


# --- agent class -------------------------------------------------------

#: Party's operational identity. The Harem World line is Nyla on the
#: chained STT/LLM/TTS pipeline, so the config mirrors Nyla's — same
#: Musubi namespace (``nyla/voice``) and same bearer token. If the
#: Party line ever gets its own identity, fork this config, give it
#: a distinct ``<agent>/voice`` prefix, and mint a dedicated token.
#:
#: No ``household_presences`` — Party is a voice channel, not a
#: surveying persona. She doesn't need the household-status tool.
PARTY_CONFIG = AgentConfig(
    agent_name="nyla",
    memory_agent_tag="nyla-voice",
    discord_room=NYLA_DISCORD_ROOM,
    allowed_delegation_targets=None,
    musubi_v2_namespace="nyla/voice",
    musubi_v2_presence="nyla/voice",
)


class PartyAgent(
    CoreToolsMixin,
    MusubiToolsMixin,
    SessionsToolsMixin,
    AcademyToolsMixin,
    Agent,
):
    """Harem World agent with full OpenClaw platform tool set.

    Config matches Nyla's because the Harem World line is Nyla on the
    chained pipeline — same person, different voice engine.
    """

    config = PARTY_CONFIG

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
        # Party uses the chained text-LLM pipeline (Gemini text + Whisper STT
        # + ElevenLabs TTS). generate_reply() is not supported at session
        # start without a preceding user turn, so the greeting has to be a
        # fixed string via session.say().
        #
        # Per Eric's feedback (2026-04-27): no formulaic recall callbacks in
        # the opener — they read as calculated. Keep this one short, warm,
        # and open-ended; let the conversation establish the texture.
        await self.session.say("Hey Eric, what's going on?")


# --- server + session --------------------------------------------------
server = AgentServer(port=8083)


@server.rtc_session(agent_name="phone-party")
async def entrypoint(ctx: JobContext) -> None:
    logger.info("phone-party entrypoint: room=%s", ctx.room.name)
    trace(f"entrypoint room={ctx.room.name}")

    await ctx.connect()

    caller = await resolve_caller(ctx)
    caller_from = caller.caller_from
    call_sid = caller.call_id
    logger.info(
        "phone-party caller resolved: from=%s call_id=%s source=%s",
        caller_from,
        call_sid,
        caller.source,
    )
    trace(f"caller source={caller.source} from={caller_from!r} call_id={call_sid!r}")

    stt = openai_plugin.STT(model="whisper-1", language="en")

    vad = silero_plugin.VAD.load(
        min_speech_duration=0.1,
        min_silence_duration=0.65,
        prefix_padding_duration=0.4,
    )

    llm = google_plugin.LLM(model=_CHAINED_LLM_MODEL, temperature=0.8)

    tts = elevenlabs_plugin.TTS(
        voice_id=_ELEVENLABS_VOICE_ID,
        model=_ELEVENLABS_MODEL,
        language="en",
    )

    extra_tools = [
        EndCallTool(
            delete_room=True,
            # end_instructions=None suppresses EndCallTool's redundant
            # goodbye — the model already says its own goodbye when it
            # decides to end the call. With the default value, the tool
            # forces a SECOND goodbye line before hanging up, producing
            # the "talked over itself" effect Eric noticed (2026-04-27).
            end_instructions=None,
        ),
    ]

    agent = PartyAgent(
        instructions=_load_persona(),
        caller_from=caller_from,
        extra_tools=extra_tools,
    )

    transcript_sid = call_sid
    if not transcript_sid and ctx.room.name.startswith("phone-"):
        transcript_sid = ctx.room.name.removeprefix("phone-")

    audio_recording = await start_call_audio_recording(
        ctx, call_sid=transcript_sid, agent_name="phone-party"
    )
    wire_call_audio_attachment(ctx, audio_recording)

    session = AgentSession(stt=stt, vad=vad, llm=llm, tts=tts)
    wire_transcript_logging(session, transcript_sid, agent_name="phone-party")
    wire_telemetry_capture(session, transcript_sid, agent_name="phone-party")
    wire_postcall_review(session, transcript_sid, agent_name="phone-party")
    wire_postcall_memory(
        session,
        call_sid=transcript_sid,
        namespace=f"{PARTY_CONFIG.musubi_v2_namespace}/episodic"
        if PARTY_CONFIG.musubi_v2_namespace
        else None,
        speaker_tag=PARTY_CONFIG.memory_agent_tag,
    )
    wire_otel_shutdown_flush(ctx)
    await session.start(agent=agent, room=ctx.room)
    attach_current_span_metadata(
        session_id=transcript_sid,
        enduser_id=caller_from,
        dialed_number=caller.dialed_number,
        caller_source=caller.source,
        lk_job_id=getattr(ctx.job, "id", None),
    )
    annotate_call_audio_recording(audio_recording)
    trace("party session: silero-vad -> whisper-1 -> gemini-3.1-flash-lite -> elevenlabs")

    trace("party: entrypoint complete, greeting scheduled via on_enter")


if __name__ == "__main__":
    cli.run_app(server)
