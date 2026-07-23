"""Sumi voice agent — forked from Party (the chained STT/LLM/TTS scaffold).

Registers as "phone-sumi" with LiveKit. This is Slice 2 of the Sumi
integration: her IDENTITY and PACKAGE only. The pipeline below is still the
inherited CHAINED cloud scaffold (Whisper / Silero / Gemini / ElevenLabs) so
the package compiles and tests; it is NOT run for Sumi. Her local pipeline —
Parakeet STT, Momo LLM, voicebook-stream TTS in her own master voice — is wired
one component per slice (3-5). No container, worker, cloud request, or Musubi
write happens in this slice.

What IS Sumi's, now and permanently:
  - identity: agent_name "sumi", registration "phone-sumi";
  - memory: its own ``sumi/voice`` namespace + ``sumi-voice`` tag + a dedicated
    ``MUSUBI_V2_TOKEN_SUMI`` bearer (entrypoint maps it) — zero bleed into
    Party's or Nyla's buckets;
  - persona: HER frozen identity from promoted canon, fail-loud (no fallback).

Greeting uses session.say() — the chained scaffold's Gemini text LLM rejects
generate_reply() at session start. Slices 3-5 replace the pipeline.
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
from sdk.config import AgentConfig, assert_agent_identity
from sdk.env import load_env
from sdk.musubi_client import wire_musubi_shutdown
from sdk.postcall import wire_postcall_review
from sdk.postcall_memory import wire_postcall_memory
from sdk.telemetry import wire_telemetry_capture
from sdk.telephony import resolve_caller
from sdk.trace import trace
from sdk.tracing import attach_current_span_metadata, wire_otel_shutdown_flush
from sdk.transcript import wire_transcript_logging
from tools.core import CoreToolsMixin
from tools.memory import MusubiToolsMixin

# --- env ---------------------------------------------------------------
load_env()

logger = logging.getLogger("voice.agent")

# --- SCAFFOLD pipeline (inherited from Party; NOT Sumi's) --------------
# These are placeholders that keep the package importable/testable. Sumi's real
# pipeline replaces them one slice at a time and is never run on the cloud
# providers below:
#   Slice 3 STT: openai Whisper -> Parakeet/Riva (streaming) via the official
#                livekit NVIDIA/Riva plugin.
#   Slice 4 LLM: gemini -> Momo (local readable route).
#   Slice 5 TTS: elevenlabs (Nyla's id) -> voicebook-stream in Sumi's own
#                accepted master voice (canon/people/sumi/voicebook).
_ELEVENLABS_VOICE_ID = "AEW6JTgnyoPaoB9zlK3S"  # SCAFFOLD ONLY — Nyla's id; Slice 5 swaps to Sumi's master
_ELEVENLABS_MODEL = "eleven_flash_v2_5"
_CHAINED_LLM_MODEL = "gemini-3.1-flash-lite-preview"  # SCAFFOLD ONLY — Slice 4 swaps to Momo

# --- persona -----------------------------------------------------------
_PROMPTS_DIR = Path(__file__).resolve().parent.parent / "prompts"

# Sumi's opener. Composed and dry, but RELATIONAL — not a service desk. Her first
# breath should open a conversation, not solicit a task (Yua's correction: an
# ending like "tell me what you need" grades her belonging by usefulness). "It's
# quiet tonight" is her characteristic environmental notice; "I'm glad you called"
# is warmth held in restraint. Reviewed WITH the persona as one identity unit;
# Eric gives the literal PASS (Yua's alt: "Good evening, Eric. I'm here. How are you?").
_GREETING = "Good evening, Eric. I'm here. How are you?"


def _load_persona(prompts_dir: Path | None = None) -> str:
    """Load Sumi's frozen identity. FAIL LOUD on a missing or empty prompt —
    there is deliberately NO generic fallback. An agent that would speak with a
    default persona instead of Sumi's is not Sumi; refusing to start is the
    honest failure (mirrors the entrypoint's crash-loop-on-empty-token stance).
    """
    path = (prompts_dir or _PROMPTS_DIR) / "system.md"
    if not path.exists():
        raise RuntimeError(
            f"Sumi persona missing: {path}. Refusing to start on a generic fallback — "
            f"her identity must be explicit or she does not speak."
        )
    text = path.read_text(encoding="utf-8").strip()
    if not text:
        raise RuntimeError(
            f"Sumi persona empty: {path}. Refusing to start on a generic fallback."
        )
    return text


# --- agent class -------------------------------------------------------

#: Sumi's operational identity. Memory is HER OWN — ``sumi/voice`` namespace,
#: ``sumi-voice`` tag, dedicated ``MUSUBI_V2_TOKEN_SUMI`` bearer — with zero
#: overlap with Party (``party/voice``) or Nyla. registration_name derives to
#: ``phone-sumi``.
SUMI_CONFIG = AgentConfig(
    agent_name="sumi",
    memory_agent_tag="sumi-voice",
    musubi_v2_namespace="sumi/voice",
)

# Fail loud at startup if $AGENT / VOICE_AGENT_NAME disagrees with the config.
assert_agent_identity(SUMI_CONFIG)


class SumiAgent(
    CoreToolsMixin,
    MusubiToolsMixin,
    Agent,
):
    """Sumi — the background maid process / archivist, on core + Musubi memory.

    Kuudere composure; care through maintenance; "I do not delete, I archive."
    Memory identity is her own (``sumi/voice``).
    """

    config = SUMI_CONFIG

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
        # Fixed-string greeting via session.say() (the scaffold text LLM cannot
        # generate_reply() at session start). Sumi's opener, not Party's.
        await self.session.say(_GREETING)


# --- server + session --------------------------------------------------
server = AgentServer(port=8083)


@server.rtc_session(agent_name=SUMI_CONFIG.registration_name)
async def entrypoint(ctx: JobContext) -> None:
    reg = SUMI_CONFIG.registration_name
    logger.info("%s entrypoint: room=%s", reg, ctx.room.name)
    trace(f"entrypoint room={ctx.room.name}")

    await ctx.connect()

    caller = await resolve_caller(ctx)
    caller_from = caller.caller_from
    call_sid = caller.call_id
    logger.info(
        "%s caller resolved: from=%s call_id=%s source=%s",
        reg,
        caller_from,
        call_sid,
        caller.source,
    )
    trace(f"caller source={caller.source} from={caller_from!r} call_id={call_sid!r}")

    # SCAFFOLD pipeline — Slices 3-5 replace these with Parakeet / Momo /
    # voicebook-stream. Never run on the cloud providers in this slice.
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
        EndCallTool(delete_room=True, end_instructions=None),
    ]

    agent = SumiAgent(
        instructions=_load_persona(),
        caller_from=caller_from,
        extra_tools=extra_tools,
    )

    transcript_sid = call_sid
    if not transcript_sid and ctx.room.name.startswith("phone-"):
        transcript_sid = ctx.room.name.removeprefix("phone-")

    audio_recording = await start_call_audio_recording(ctx, call_sid=transcript_sid, agent_name=reg)
    wire_call_audio_attachment(ctx, audio_recording)

    session = AgentSession(stt=stt, vad=vad, llm=llm, tts=tts)
    wire_transcript_logging(session, transcript_sid, agent_name=reg)
    wire_telemetry_capture(session, transcript_sid, agent_name=reg)
    wire_postcall_review(session, transcript_sid, agent_name=reg)
    wire_postcall_memory(
        session,
        call_sid=transcript_sid,
        namespace=f"{SUMI_CONFIG.musubi_v2_namespace}/episodic"
        if SUMI_CONFIG.musubi_v2_namespace
        else None,
        speaker_tag=SUMI_CONFIG.memory_agent_tag,
    )
    wire_otel_shutdown_flush(ctx)
    wire_musubi_shutdown(ctx)
    await session.start(agent=agent, room=ctx.room)
    attach_current_span_metadata(
        session_id=transcript_sid,
        enduser_id=caller_from,
        dialed_number=caller.dialed_number,
        caller_source=caller.source,
        lk_job_id=getattr(ctx.job, "id", None),
    )
    annotate_call_audio_recording(audio_recording)
    trace("sumi SCAFFOLD session: silero-vad -> whisper-1 -> gemini -> elevenlabs (Slices 3-5 swap to local)")
    trace("sumi: entrypoint complete, greeting scheduled via on_enter")


if __name__ == "__main__":
    cli.run_app(server)
