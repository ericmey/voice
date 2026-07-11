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
import os
from pathlib import Path

from livekit.agents import AgentSession, JobContext, JobProcess, cli
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
from sdk.duplex import wire_half_duplex
from sdk.env import load_env
from sdk.liveness import wire_liveness_watchdog
from sdk.musubi_client import wire_musubi_shutdown
from sdk.postcall import wire_postcall_review
from sdk.postcall_memory import arm_postcall_memory, run_postcall_memory
from sdk.telemetry import wire_telemetry_capture
from sdk.telephony import resolve_caller
from sdk.trace import trace
from sdk.tracing import attach_current_span_metadata, wire_otel_shutdown_flush
from sdk.transcript import wire_transcript_logging
from tools.base_agent import BaseVoiceAgent, load_persona
from tools.core import CoreToolsMixin
from tools.memory import MusubiToolsMixin

# --- env ---------------------------------------------------------------
load_env()

logger = logging.getLogger("voice.agent")

# Sumi's fully-local inference services — Riva ASR, Mistral Nemo, Orpheus TTS — all on
# mizuki's Blackwell card, reached from the agent container via the host's LAN IP.
#
# This was a bare `_SUMI_HOST = "10.0.20.25"` literal: her ENTIRE voice chain pinned to one
# hardcoded IP, while every other endpoint in the repo (LiveKit, Musubi, the OTel collector)
# comes from the environment. Move the box, renumber the LAN, or stand her up anywhere else,
# and she breaks in a way that requires a code change and an image rebuild to fix.
#
# The default is the current value, so nothing changes today. But it is now a knob, not a
# weld — and a knob is what every other service in this stack already has.
_SUMI_HOST = os.environ.get("SUMI_INFERENCE_HOST", "10.0.20.25")
_RIVA_ASR_SERVER = f"{_SUMI_HOST}:50051"  # Riva Parakeet ASR (gRPC)
_NEMO_BASE_URL = f"http://{_SUMI_HOST}:8090/v1"  # Mistral Nemo via llama.cpp
_ORPHEUS_BASE_URL = f"http://{_SUMI_HOST}:5005/v1"  # Orpheus TTS (OpenAI-compatible)

# --- persona -----------------------------------------------------------
#
# Sumi used to carry her OWN copy of this loader, with her OWN silent fallback
# ("You are the Harem World host on a phone call with Eric"). Two implementations of the
# identity path, with two different strangers to become. She now uses the one shared loader
# in tools.base_agent, which raises rather than substituting anybody.
_PROMPTS_DIR = Path(__file__).resolve().parent.parent / "prompts"


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


# Sumi composes exactly like her sisters — the tools she has are stated HERE, on her.
#
# She differs from them in ONE way that matters: she runs a chained STT->LLM->TTS pipeline
# rather than a Gemini realtime model, so she cannot `generate_reply()` at session start and
# needs her own greeting. That is why she inherits `BaseVoiceAgent` (model-agnostic) rather
# than `BaseRealtimeAgent`.
#
# She used to inherit `Agent` directly and re-implement the base's `__init__` byte-for-byte,
# plus her own persona loader and her own divergent defaults — not because she needed to, but
# because `BaseRealtimeAgent` baked in a tool set she could not opt out of. Splitting the base
# removed the reason. This is the correct amount of divergence: her pipeline and her greeting.
class SumiAgent(
    CoreToolsMixin,
    MusubiToolsMixin,
    BaseVoiceAgent,
):
    """Sumi Tachibana — core + Musubi memory tools on the chained voice line.

    Persona is Sumi (bright, chipper anime maid — smiles and emotes, and
    loves keeping everything); memory identity is her own (``sumi/voice``).
    """

    config = SUMI_CONFIG

    async def on_enter(self) -> None:
        # KNOWN DIVERGENCE, deliberately NOT fixed blind — validate on a real call.
        #
        # Her three sisters prefetch `fetch_recent_context(limit=10)` and pass it to
        # `generate_reply()` as background awareness. Sumi does not, so she starts a call
        # COLDER than they do: she has `musubi_recent` as a tool and can reach for it, but it
        # is not already in her hands.
        #
        # She cannot simply copy them — `generate_reply()` is unsupported at session start on a
        # chained pipeline, so her greeting is a fixed `session.say()` string with nowhere to
        # fold context into.
        #
        # The obvious move — inject the context into her chat context instead — is exactly the
        # move that has already broken her once: 69b3d99 ("keep the greeting out of LLM
        # context") was REVERTED by 630380d because it stalled the greeting/turn loop. Mistral
        # Nemo's alternation constraints make her chat context genuinely load-bearing.
        #
        # So this is a real gap with a real reason to be careful. It gets a call, not a guess.
        #
        # Sumi uses the chained text-LLM pipeline (Nemo text + Riva STT +
        # Orpheus TTS). generate_reply() is not supported at session start
        # without a preceding user turn, so the greeting has to be a fixed
        # string via session.say().
        #
        # Per Eric's feedback (2026-04-27): no formulaic recall callbacks in
        # the opener — they read as calculated. Sumi opens bright and glad to
        # hear him — a warm, chipper hello, then she's all his.
        #
        # The greeting is a normal assistant turn (in chat ctx). Mistral Nemo's
        # leading-assistant / alternation constraints are handled server-side
        # by the lenient chat template on llama-nemo (--chat-template-file), so
        # no client-side add_to_chat_ctx workaround is needed here.
        await self.session.say("Eric! Hi hi~ there you are — what can I do for you?")


# --- endpointing -------------------------------------------------------
#
# How long Sumi waits in silence before deciding Eric has finished his turn.
#
# Her three sisters use 1000ms, with an explicit reason stated in tools/base_agent.py:
# "Eric can pause up to 1s mid-thought without Gemini ending his turn." That reason is about
# ERIC — his speech pattern — not about Gemini. It applies to Sumi identically.
#
# She was at 0.65s: 350ms MORE trigger-happy than her sisters, on the chained STT->LLM->TTS
# pipeline, which has LESS context than a realtime model to recover from cutting him off
# mid-sentence. Nothing in the code said why. Best guess: a leftover from debugging her
# dead-air bugs (04739ef, 69b3d99, 630380d).
#
# Aligned to 1.0s. VALIDATE ON THE REAL CALL — if she now feels sluggish to respond, this is
# the first dial to turn, and the fix is to add a semantic turn detector rather than to race
# the silence timer.
_MIN_SILENCE_DURATION_S = 1.0


# --- prewarm -----------------------------------------------------------
#
# Silero VAD is an ONNX model. It was being loaded INSIDE the entrypoint, AFTER
# `ctx.connect()` — so the caller was already on the line, listening to nothing, while the
# model came off disk. That is the canonical LiveKit anti-pattern; every chained-pipeline
# example in their docs loads VAD in prewarm and stashes it in `proc.userdata`.
#
# `setup_fnc` runs once per job PROCESS, before any job is assigned, with its own 10s
# `initialize_process_timeout`. `proc.userdata` is per-process — which is exactly right for
# a model (a shared, immutable, expensive resource). Per-CALL state must never go here; a
# job process can be reused for sequential jobs, so that would leak between callers.


def prewarm(proc: JobProcess) -> None:
    """Load the VAD model before a call arrives, not while one is waiting."""
    proc.userdata["vad"] = silero_plugin.VAD.load(
        min_speech_duration=0.1,
        min_silence_duration=_MIN_SILENCE_DURATION_S,
        prefix_padding_duration=0.4,
    )
    logger.info("prewarm: silero VAD loaded (min_silence=%.2fs)", _MIN_SILENCE_DURATION_S)


# --- server + session --------------------------------------------------
# Worker resource posture — set EXPLICITLY, not left to the library defaults.
#
# `num_idle_processes` defaults to 8 in prod. That is 8 pre-forked job processes PER AGENT —
# 32 across the four of them, on one 12-core box. And `job_memory_limit_mb` defaults to 0,
# i.e. DISABLED: a job that leaks has no ceiling and takes the host down with it, along with
# the other three agents and the phone line.
#
# That was an unbounded posture before prewarm. It is worse WITH prewarm, and that is my
# doing: `setup_fnc` runs per job PROCESS, so every idle process now loads its own copy of
# the model. Eight idle Silero VADs per agent is not a warm pool, it is a memory leak with a
# schedule.
#
# This is a personal phone line, not a call centre. ONE warm process per agent is the entire
# point of prewarm — the next call is instant, and the pool refills behind it. The memory
# ceiling is deliberately generous: it exists to kill a runaway, never to fire in normal
# operation. Sumi is the heavy one (Riva + Silero + Orpheus) and idles around 800MB.
#
# WATCH ON THE FIRST CALL: if the pool of 1 is exhausted by back-to-back calls, a second
# caller pays the prewarm cost. Raise it before raising the memory ceiling.
IDLE_PROCESSES = 1
JOB_MEMORY_LIMIT_MB = 4096
server = AgentServer(
    port=8083,
    setup_fnc=prewarm,
    num_idle_processes=IDLE_PROCESSES,
    job_memory_limit_mb=JOB_MEMORY_LIMIT_MB,
)


@server.rtc_session(
    agent_name=SUMI_CONFIG.registration_name,
    # Post-call extraction runs here, NOT in a shutdown callback: on_session_end
    # fires BEFORE `ShuttingDown` starts the 10s kill clock, and gets the 300s
    # `session_end_timeout`. It replaces a detached subprocess whose only job was
    # to outlive a kill that no longer happens.
    on_session_end=run_postcall_memory,
)
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

    # Prewarmed in `setup_fnc` — no ONNX load while the caller is waiting.
    vad = ctx.proc.userdata["vad"]

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
        instructions=load_persona(_PROMPTS_DIR),
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

    # HALF-DUPLEX: close the caller's mic while she speaks. On a speakerphone her own voice
    # returns through his microphone and arrives as caller input — that is what wedged the
    # 2026-07-11 call. Costs barge-in; prevents the loop. (sdk/duplex.py)
    wire_half_duplex(session, call_sid=transcript_sid, agent_name=reg)

    # DEAD-AIR WATCHDOG: a silent call must never look like a healthy one. Runs on its own
    # clock, and user VAD deliberately CANNOT satisfy it — on that call the VAD was being fed
    # by her own echo, so a watchdog trusting it would have been kept alive by the very fault
    # it exists to catch. (sdk/liveness.py)
    wire_liveness_watchdog(session, ctx, call_sid=transcript_sid, agent_name=reg)
    arm_postcall_memory(
        ctx,
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
