"""Sumi voice agent — chained STT/LLM/TTS, the Harem World line.

Registers as "phone-sumi" with LiveKit. Runs Sumi Tachibana's fully-local
inference stack on mizuki's Blackwell card — separate components:
  - STT: NVIDIA Riva Parakeet ASR (gRPC, needs Silero VAD)
  - VAD: Silero (segments caller audio into utterances)
  - LLM: Mistral Nemo via llama.cpp (OpenAI-compatible)
  - TTS: Orpheus (OpenAI-compatible; voice ``tara`` is a placeholder
    until Sumi's own low/dry voice is cloned)

Inherits the voice tool set (Core, Memory). Persona is Sumi (the bright,
chipper anime maid — all smiles and emotes, who joyfully keeps everything);
memory is her own (``sumi/voice`` / ``sumi-voice``), distinct from her fleet
presence (``sumi/hermes``) — one Sumi, two channels.

Greeting uses session.say() — the chained text LLM rejects
generate_reply() at session start (tools without a preceding user turn).
"""

from __future__ import annotations

import logging
from pathlib import Path

from livekit.agents import Agent, AgentSession, JobContext, cli
from livekit.agents.beta import EndCallTool
from livekit.agents.worker import AgentServer
from livekit.plugins import nvidia as nvidia_plugin
from livekit.plugins import openai as openai_plugin
from livekit.plugins import silero as silero_plugin
from orpheus_tts import OrpheusTTS
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

# Sumi's fully-local inference services, all on mizuki's Blackwell card, reached
# from the agent container via the host's LAN IP.
_SUMI_HOST = "10.0.20.25"
_RIVA_ASR_SERVER = f"{_SUMI_HOST}:50051"  # Riva Parakeet ASR (gRPC)
_NEMO_BASE_URL = f"http://{_SUMI_HOST}:8090/v1"  # Mistral Nemo via llama.cpp
_ORPHEUS_BASE_URL = f"http://{_SUMI_HOST}:5005/v1"  # Orpheus TTS (OpenAI-compatible)

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

#: Sumi's operational identity. The Harem World line runs her fully-local
#: chained STT/LLM/TTS pipeline. Her *memory* is her own — ``sumi/voice``
#: namespace, ``sumi-voice`` tag, and a dedicated ``MUSUBI_V2_TOKEN_SUMI``
#: bearer (entrypoint maps it). This is her voice presence; her fleet
#: presence (``sumi/hermes``) is a separate channel of the same Sumi.
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
    """Sumi Tachibana — core + Musubi memory tools on the chained voice line.

    Persona is Sumi (bright, chipper anime maid — smiles and emotes, and
    loves keeping everything); memory identity is her own (``sumi/voice``).
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
        # Sumi uses the chained text-LLM pipeline (Nemo text + Riva STT +
        # Orpheus TTS). generate_reply() is not supported at session start
        # without a preceding user turn, so the greeting has to be a fixed
        # string via session.say().
        #
        # Per Eric's feedback (2026-04-27): no formulaic recall callbacks in
        # the opener — they read as calculated. Sumi opens bright and glad to
        # hear him — a warm, chipper hello, then she's all his.
        await self.session.say("Eric! Hi hi~ there you are — what can I do for you?")


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

    # Sumi's fully-local chained pipeline (all on mizuki's Blackwell card):
    # Riva ASR (STT) -> Mistral Nemo / llama.cpp (LLM) -> Orpheus (TTS).
    # The Riva CTC NIM serves "parakeet-1.1b-en-US-asr-streaming"; the plugin's
    # default (…-silero-vad-sortformer) is NOT loaded and fails with
    # INVALID_ARGUMENT "model not available", so name it explicitly.
    stt = nvidia_plugin.STT(
        server=_RIVA_ASR_SERVER,
        model="parakeet-1.1b-en-US-asr-streaming",
        use_ssl=False,
        language_code="en-US",
    )

    vad = silero_plugin.VAD.load(
        min_speech_duration=0.1,
        min_silence_duration=0.65,
        prefix_padding_duration=0.4,
    )

    llm = openai_plugin.LLM(model="nemo", base_url=_NEMO_BASE_URL, api_key="sk-local")

    # Orpheus-FastAPI isn't OpenAI-streaming-compatible (it ignores
    # response_format and returns a whole WAV), so the stock openai.TTS pushes
    # no frames -> dead air. OrpheusTTS reads the WAV and pushes raw PCM.
    tts = OrpheusTTS(base_url=_ORPHEUS_BASE_URL, voice="tara")

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
    trace("sumi session: silero-vad -> riva-parakeet -> mistral-nemo -> orpheus")

    trace("sumi: entrypoint complete, greeting scheduled via on_enter")


if __name__ == "__main__":
    cli.run_app(server)
