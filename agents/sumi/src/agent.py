"""Sumi voice agent — forked from Party (the chained STT/LLM/TTS scaffold).

Registers as "phone-sumi" with LiveKit. This is Slice 2 of the Sumi
integration: her IDENTITY and PACKAGE only. The pipeline below is still the
inherited CHAINED cloud scaffold (Whisper / Silero / Gemini / ElevenLabs) so
the package compiles and tests; it is NOT run for Sumi. Her local pipeline —
Parakeet STT (Slice 3, done), Momo LLM (Slice 4, done), voicebook-stream TTS in
her own master voice (Slice 5) — is wired one component per slice. STT and the
LLM now reach local self-hosted services; only TTS remains the inherited cloud
scaffold. No container, worker, or Musubi write happens in these wiring slices.

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
import os
from pathlib import Path

from livekit.agents import Agent, AgentSession, APIConnectOptions, JobContext, cli
from livekit.agents.beta import EndCallTool
from livekit.agents.voice.agent_session import SessionConnectOptions
from livekit.agents.worker import AgentServer
from livekit.plugins import elevenlabs as elevenlabs_plugin
from livekit.plugins import nvidia as nvidia_plugin
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
# What remains scaffold: only TTS. It is a placeholder that keeps the package
# importable/testable and is never run on the cloud provider below:
#   Slice 5 TTS: elevenlabs (Nyla's id) -> voicebook-stream in Sumi's own
#                accepted master voice (canon/people/sumi/voicebook).
# (Slice 3 STT and Slice 4 LLM are done — both now reach local self-hosted
#  services; see the LOCAL blocks below.)
_ELEVENLABS_VOICE_ID = "AEW6JTgnyoPaoB9zlK3S"  # SCAFFOLD ONLY — Nyla's id; Slice 5 swaps to Sumi's master
_ELEVENLABS_MODEL = "eleven_flash_v2_5"

# Slice 3 — LOCAL STT: self-hosted Parakeet/Riva via the official LiveKit NVIDIA plugin
# (streaming). Reaches parakeet-ctl:50051 by service DNS on voice_default; insecure — the
# self-hosted Riva speaks plaintext gRPC (no TLS, no api-key). 16 kHz mono is the model's
# contract (streaming transcription proven word-for-word 2026-07-23; offline mode unsupported).
# The plugin's default model name (…-silero-vad-sortformer) is NOT what our self-hosted
# NIM serves — parakeet-ctl advertises exactly one ASR model, `parakeet-1.1b-en-US-asr-
# streaming` (streaming/online/16kHz/en-US), verified via GetRivaSpeechRecognitionConfig.
# Using the plugin default would fail "model unavailable"; pin the served name.
_STT_SERVER = os.environ.get("SUMI_STT_SERVER", "parakeet-ctl:50051")
_STT_MODEL = os.environ.get("SUMI_STT_MODEL", "parakeet-1.1b-en-US-asr-streaming")

# Slice 4 — LOCAL LLM: Sumi's mind is Momo (qwen3.6-35b-a3b) via the explicit
# LiteLLM `sumi` route, reached OpenAI-compatibly at the proven voice_default
# endpoint http://10.0.20.25:4000/v1 (TCP + HTTP 200 verified from the network).
# The route is deliberately CONSTRAINED for a live phone turn (created/proven
# 2026-07-23):
#   - no-think: the backend is a reasoning model that, left in thinking mode,
#     emits reasoning_content with an EMPTY content field. Voice needs a spoken
#     content field, so the route pins chat_template_kwargs enable_thinking:false
#     (proven finish=stop, reason_len=0, TTFT ~0.32s / total ~0.39s);
#   - no duplicate spoken turns: the route is num_retries:0 AND we set
#     max_retries=0 here — a retried completion could speak the same words twice;
#   - NO cloud fallback: the route has no fallback group, so a Momo outage yields
#     a hard error ("No fallback model group found"), never a silent escape to a
#     cloud model. Sumi goes quiet before she speaks as something that isn't her.
_LLM_BASE_URL = os.environ.get("SUMI_LLM_BASE_URL", "http://10.0.20.25:4000/v1")
_LLM_MODEL = os.environ.get("SUMI_LLM_MODEL", "sumi")


def _llm_api_key() -> str:
    """LiteLLM bearer for the `sumi` route. FAIL LOUD if absent — Sumi's LLM has
    no cloud fallback and must not silently start on a default/empty key. Mirrors
    the persona's refuse-to-start stance: explicit or she does not speak."""
    key = os.environ.get("SUMI_LLM_API_KEY") or os.environ.get("LITELLM_API_KEY")
    if not key:
        raise RuntimeError(
            "SUMI_LLM_API_KEY (LiteLLM bearer for the 'sumi' route) is unset. "
            "Refusing to start — Sumi's LLM has no cloud fallback and will not "
            "use a default or empty key."
        )
    return key

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

    # Slice 3 — LOCAL STT: Parakeet/Riva streaming via the official NVIDIA plugin (was
    # Whisper). LLM + TTS below are still the inherited SCAFFOLD (Slices 4-5 swap them to
    # Momo + voicebook-stream); never run on the cloud providers in this slice.
    stt = nvidia_plugin.STT(
        server=_STT_SERVER,
        use_ssl=False,
        api_key="",
        model=_STT_MODEL,
        language_code="en-US",
        sample_rate=16000,
        punctuate=True,
    )
    vad = silero_plugin.VAD.load(
        min_speech_duration=0.1,
        min_silence_duration=0.65,
        prefix_padding_duration=0.4,
    )
    # Slice 4 — LOCAL LLM: Momo via the explicit LiteLLM `sumi` route (was the
    # gemini scaffold). max_retries=0 so a phone turn is never spoken twice; the
    # route itself is num_retries:0 with no cloud fallback (see the block above).
    llm = openai_plugin.LLM(
        model=_LLM_MODEL,
        base_url=_LLM_BASE_URL,
        api_key=_llm_api_key(),
        temperature=0.7,
        max_retries=0,
        timeout=30,
    )
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

    # LLM retries at the livekit layer re-run the WHOLE generation and can
    # re-emit already-streamed tokens — a duplicated SPOKEN turn on the phone.
    # The default is max_retry=3 (4 attempts). The route is num_retries:0 and the
    # openai client is max_retries=0, but ONLY llm_conn_options governs this
    # layer, so pin it to 0: exactly one LLM attempt, no double-speak. (STT/TTS
    # keep the default 3 — their retries don't duplicate an LLM turn.)
    session = AgentSession(
        stt=stt,
        vad=vad,
        llm=llm,
        tts=tts,
        conn_options=SessionConnectOptions(
            llm_conn_options=APIConnectOptions(max_retry=0),
        ),
    )
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
    trace("sumi session: silero-vad -> parakeet-riva(local STT) -> momo/sumi-route(local LLM) -> elevenlabs(scaffold)")
    trace("sumi: entrypoint complete, greeting scheduled via on_enter")


if __name__ == "__main__":
    cli.run_app(server)
