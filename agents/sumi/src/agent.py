"""Sumi voice agent — forked from Party, now on a FULLY LOCAL pipeline.

Registers as "phone-sumi" with LiveKit. Her pipeline is her own, end to end, and
runs entirely on self-hosted services — no cloud provider on the speaking path:
  - Slice 3 STT: faster-distil-whisper-large-v3 via local Speaches + LiveKit's
    maintained batch-to-stream adapter;
  - Slice 4 LLM: Momo (qwen3.6-35b-a3b) via the explicit LiteLLM `sumi` route;
  - Slice 5 TTS: voicebook-stream in Sumi's own accepted master voice (`sumi-v1`).
The inherited Gemini/ElevenLabs scaffold is gone. (VAD stays silero — local.)

What IS Sumi's, now and permanently:
  - identity: agent_name "sumi", registration "phone-sumi";
  - memory: its own ``sumi/voice`` namespace + ``sumi-voice`` tag + a dedicated
    ``MUSUBI_V2_TOKEN_SUMI`` bearer (entrypoint maps it) — zero bleed into
    Party's or Nyla's buckets;
  - persona: HER frozen identity from promoted canon, fail-loud (no fallback).

Greeting uses session.say() — a deterministic fixed opener (Sumi's exact first
line), not a generated reply, so her first breath is always hers verbatim.
"""

from __future__ import annotations

import asyncio
import logging
import math
import os
from collections.abc import AsyncGenerator, AsyncIterable, AsyncIterator
from pathlib import Path

import httpx
import numpy as np
from livekit import rtc
from livekit.agents import (
    Agent,
    AgentSession,
    APIConnectOptions,
    FlushSentinel,
    JobContext,
    ModelSettings,
    TurnHandlingOptions,
    cli,
)
from livekit.agents import (
    llm as livekit_llm,
)
from livekit.agents import stt as livekit_stt
from livekit.agents.beta import EndCallTool
from livekit.agents.voice.agent_session import SessionConnectOptions
from livekit.agents.worker import AgentServer
from livekit.plugins import elevenlabs as elevenlabs_plugin
from livekit.plugins import nvidia as nvidia_plugin
from livekit.plugins import openai as openai_plugin
from livekit.plugins import silero as silero_plugin
from pedalboard import Gain, HighpassFilter, PeakFilter
from pedalboard._pedalboard import Pedalboard
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
from sherpa_stt import SherpaSTT
from tools.core import CoreToolsMixin
from tools.memory import MusubiToolsMixin
from voicebook_tts import build_streaming_voicebook_tts

# --- env ---------------------------------------------------------------
load_env()

logger = logging.getLogger("voice.agent")

# Slice 5 — LOCAL TTS: Sumi's own master voice via the managed voicebook-stream
# service (custom LiveKit TTS adapter in voicebook_tts.py). Reaches
# voicebook-stream:5060 by service DNS on voice_default; raw s16le PCM @ 24kHz mono.
# `sumi-v1` is her frozen registered voice (server-owned registry; an unknown id
# fails loud 404 — she never speaks in a substitute voice). Base URL + voice id
# are env-overridable.
_TTS_VOICE_ID = os.environ.get("SUMI_TTS_VOICE_ID", "sumi-v1")
_TTS_BASE_URL = os.environ.get("SUMI_TTS_BASE_URL", "http://voicebook-stream:5060")
_TTS_PROVIDER = os.environ.get("SUMI_TTS_PROVIDER", "voicebook").strip().lower()
_ELEVENLABS_VOICE_ID = os.environ.get("SUMI_ELEVENLABS_VOICE_ID", "AEW6JTgnyoPaoB9zlK3S")
_ELEVENLABS_MODEL = os.environ.get("SUMI_ELEVENLABS_MODEL", "eleven_flash_v2_5")
_KOKORO_BASE_URL = os.environ.get("SUMI_KOKORO_BASE_URL", "http://kokoro-fastapi:8880/v1")
_KOKORO_VOICE = os.environ.get("SUMI_KOKORO_VOICE", "af_heart")

# Eric's accepted Kokoro mastering curve (blind sample B, 2026-07-24).  Keep
# this provider-independent: the hook processes LiveKit PCM frames after TTS,
# so switching an explicitly selected provider does not fork the phone mastering
# contract.  Set SUMI_TELEPHONY_MASTERING=0 for an exact bypass A/B.
_MASTERING_ENABLED = os.environ.get("SUMI_TELEPHONY_MASTERING", "1").strip().lower() not in {
    "0",
    "false",
    "no",
    "off",
}
_MASTERING_GAIN_DB = 1.558
_MASTERING_PEAK_LIMIT = 10.0 ** (-3.0 / 20.0)
_MASTERING_RELEASE_MS = 100.0


def _build_telephony_mastering_board() -> Pedalboard:
    """Return a fresh, stateful instance of the accepted sample-B curve."""

    return Pedalboard(
        [
            HighpassFilter(cutoff_frequency_hz=180.0),
            PeakFilter(cutoff_frequency_hz=350.0, gain_db=-2.25, q=0.70),
            PeakFilter(cutoff_frequency_hz=900.0, gain_db=1.0, q=0.75),
            PeakFilter(cutoff_frequency_hz=2400.0, gain_db=3.0, q=0.90),
            PeakFilter(cutoff_frequency_hz=3250.0, gain_db=1.0, q=1.0),
            Gain(gain_db=_MASTERING_GAIN_DB),
        ]
    )


class _PeakEnvelopeLimiter:
    """Continuous peak ceiling with instantaneous attack and smooth release."""

    def __init__(self) -> None:
        self._peak_envelope = 0.0

    def process(self, audio: np.ndarray, sample_rate: int) -> np.ndarray:
        limited = audio.copy()
        release_samples = sample_rate * (_MASTERING_RELEASE_MS / 1000.0)
        release_decay = math.exp(-1.0 / release_samples)
        for index in range(limited.shape[1]):
            sample_peak = float(np.max(np.abs(limited[:, index]), initial=0.0))
            self._peak_envelope = max(sample_peak, self._peak_envelope * release_decay)
            if self._peak_envelope > _MASTERING_PEAK_LIMIT:
                limited[:, index] *= _MASTERING_PEAK_LIMIT / self._peak_envelope

        return limited


class _TelephonyMasteringProcessor:
    """Stateful phone mastering with click-free overload containment."""

    def __init__(self) -> None:
        self._board = _build_telephony_mastering_board()
        self._limiter = _PeakEnvelopeLimiter()

    def process(self, audio: np.ndarray, sample_rate: int) -> tuple[np.ndarray, float]:
        """Apply the accepted curve and a continuous peak-release envelope."""

        processed = np.asarray(self._board(audio, sample_rate, reset=False), dtype=np.float32)
        if processed.shape != audio.shape:
            raise RuntimeError(
                "telephony mastering changed frame geometry: "
                f"input={audio.shape} output={processed.shape}"
            )

        input_peak = float(np.max(np.abs(processed), initial=0.0))
        return self._limiter.process(processed, sample_rate), input_peak


def _master_audio_frame(
    frame: rtc.AudioFrame, processor: _TelephonyMasteringProcessor
) -> rtc.AudioFrame:
    """Apply the stateful mastering board without changing frame geometry.

    LiveKit frames are interleaved signed-16 PCM.  Pedalboard consumes float
    arrays shaped channels x samples.  ``reset=False`` is load-bearing: filter
    state must cross LiveKit frame boundaries or the filters themselves can
    introduce clicks at every frame.
    """

    expected_samples = frame.samples_per_channel * frame.num_channels
    pcm = np.frombuffer(frame.data, dtype=np.int16)
    if pcm.size != expected_samples:
        raise RuntimeError(
            f"invalid LiveKit audio frame: expected {expected_samples} samples, received {pcm.size}"
        )

    audio = pcm.reshape(frame.samples_per_channel, frame.num_channels).T.astype(np.float32)
    audio /= 32768.0
    processed, input_peak = processor.process(audio, frame.sample_rate)
    if input_peak > _MASTERING_PEAK_LIMIT:
        # Keep the accepted -3 dBFS ceiling without changing gain abruptly at
        # LiveKit frame boundaries.  The stateful release is load-bearing:
        # frame-local rescaling produced audible zipper-like micro-dropouts in
        # longer, expressive Qwen3-TTS delivery.
        logger.warning(
            "sumi telephony mastering overload contained: input_peak=%.6f target_peak=%.6f",
            input_peak,
            _MASTERING_PEAK_LIMIT,
        )

    interleaved = np.rint(processed.T.reshape(-1) * 32768.0)
    encoded = np.clip(interleaved, -32768.0, 32767.0).astype(np.int16).tobytes()
    return rtc.AudioFrame(
        data=encoded,
        sample_rate=frame.sample_rate,
        num_channels=frame.num_channels,
        samples_per_channel=frame.samples_per_channel,
        userdata=dict(frame.userdata),
    )


def build_tts():
    """Build Sumi's explicitly selected TTS provider; never silently fall back."""

    if _TTS_PROVIDER == "voicebook":
        return build_streaming_voicebook_tts(voice_id=_TTS_VOICE_ID, base_url=_TTS_BASE_URL)
    if _TTS_PROVIDER == "elevenlabs":
        if not _ELEVENLABS_VOICE_ID:
            raise ValueError("SUMI_ELEVENLABS_VOICE_ID is required for ElevenLabs TTS")
        return elevenlabs_plugin.TTS(
            voice_id=_ELEVENLABS_VOICE_ID,
            model=_ELEVENLABS_MODEL,
            language="en",
        )
    if _TTS_PROVIDER == "kokoro":
        # LiveKit 1.6 selects raw-audio mode only for known OpenAI audio model
        # names. Kokoro-FastAPI ignores the model selector and serves Kokoro;
        # `tts-1` therefore keeps the maintained plugin on its audio response
        # path instead of the gpt-4o SSE path.
        return openai_plugin.TTS(
            model="tts-1",
            voice=_KOKORO_VOICE,
            api_key="not-needed",
            base_url=_KOKORO_BASE_URL,
            response_format="wav",
        )
    raise ValueError(f"unsupported SUMI_TTS_PROVIDER: {_TTS_PROVIDER!r}")


# Slice 3 — LOCAL STT.  The accepted phone default is faster-distil-whisper-
# large-v3 behind Speaches, with Silero endpointing through LiveKit's maintained
# StreamAdapter.  The warmed control scored exact on the general and numeric
# fixtures with ~0.24-0.31s final-after-audio latency.  Parakeet/Riva and
# sherpa/Nemotron remain explicit rollback/evaluation providers; there is no
# automatic fallback or cloud transcription path.
_STT_PROVIDER = os.environ.get("SUMI_STT_PROVIDER", "faster-whisper").strip().lower()
_STT_SERVER = os.environ.get("SUMI_STT_SERVER", "parakeet-ctl:50051")
_STT_MODEL = os.environ.get("SUMI_STT_MODEL", "parakeet-1.1b-en-US-asr-streaming")
_SHERPA_STT_URL = os.environ.get("SUMI_SHERPA_STT_URL", "ws://sherpa-stt:6006")
_SHERPA_STT_MODEL = os.environ.get(
    "SUMI_SHERPA_STT_MODEL", "nemotron-speech-streaming-en-0.6b-560ms-int8"
)
_WHISPER_STT_BASE_URL = os.environ.get("SUMI_WHISPER_STT_BASE_URL", "http://speaches-stt:8000/v1")
_WHISPER_STT_MODEL = os.environ.get(
    "SUMI_WHISPER_STT_MODEL", "Systran/faster-distil-whisper-large-v3"
)
_WHISPER_STT_PROMPT = os.environ.get(
    "SUMI_WHISPER_STT_PROMPT",
    (
        "Expected names and terms: Sumi Katsuragi, Mizuki, Momo, LiveKit, Musubi, "
        "Aoi, Yua, Shiori, Tama, Nyla."
    ),
)


def build_stt(*, vad):
    """Build the explicitly selected local STT provider; never silently fall back."""

    if _STT_PROVIDER == "parakeet":
        return nvidia_plugin.STT(
            server=_STT_SERVER,
            use_ssl=False,
            api_key="",
            model=_STT_MODEL,
            language_code="en-US",
            sample_rate=16000,
            punctuate=True,
        )
    if _STT_PROVIDER == "sherpa":
        return SherpaSTT(url=_SHERPA_STT_URL, model=_SHERPA_STT_MODEL)
    if _STT_PROVIDER == "faster-whisper":
        # Speaches v0.9's "realtime" websocket still buffers an utterance and
        # invokes its batch transcription endpoint after server VAD.  Use the
        # maintained LiveKit StreamAdapter instead: our already-qualified
        # Silero VAD owns endpointing, and the same warmed faster-whisper batch
        # endpoint returns the final transcript without a version-specific
        # websocket compatibility fork.
        batch_stt = openai_plugin.STT(
            model=_WHISPER_STT_MODEL,
            language="en",
            prompt=_WHISPER_STT_PROMPT,
            api_key="not-needed",
            base_url=_WHISPER_STT_BASE_URL,
            use_realtime=False,
        )
        return livekit_stt.StreamAdapter(stt=batch_stt, vad=vad)
    raise ValueError(f"unsupported SUMI_STT_PROVIDER: {_STT_PROVIDER!r}")


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

# Phone turn handling.  The stock streaming defaults are too eager for the
# self-hosted Parakeet path: on the 2026-07-24 diagnostic call, untranscribed
# audio activity paused Sumi twice for exactly the default 2.0-second false-
# interruption window, and a final STT transcript arrived after a turn had
# already been committed.  Require an actual transcribed word before pausing
# playback, give the streaming transcript a little more time to settle, and
# make any remaining resumable false pause short enough not to sound like the
# call dropped.  Real one-word barge-ins ("stop", "wait") still count.
_TURN_HANDLING: TurnHandlingOptions = {
    "endpointing": {"min_delay": 0.8},
    "interruption": {
        "min_duration": 0.5,
        "min_words": 1,
        "resume_false_interruption": True,
        "false_interruption_timeout": 0.75,
    },
}

# CAPACITY-SAFETY cap — bound EVERY spoken turn to a short conversational length.
# An uncapped request lets the backend run toward its huge default n_predict
# (65536 on Momo). On a memory-thin host that pins a slot + KV and can grind
# under swap, pressuring the host to distress. A phone turn is one or two
# sentences; 64 completion tokens is plenty and makes a worker-side runaway
# generation structurally impossible. This is a SAFETY bound first, brevity
# second — it is the direct fix for the 2026-07-23 uncapped-generation incident.
_LLM_MAX_TOKENS_CEILING = 64  # HARD ceiling. The env override may only LOWER it.


def _resolve_max_tokens() -> int:
    """Per-turn output cap. Defaults to the 64-token ceiling; SUMI_LLM_MAX_TOKENS may
    only LOWER it (1..64). A value above the ceiling, non-numeric, or < 1 FAILS LOUD:
    the env must never be able to raise or defeat the safety bound. An override that
    can lift the cap is not a cap — that is the exact hole the 2026-07-23 incident
    came through, and the reason this is validated rather than a bare int()."""
    raw = os.environ.get("SUMI_LLM_MAX_TOKENS")
    if raw is None:
        return _LLM_MAX_TOKENS_CEILING
    try:
        value = int(raw)
    except (TypeError, ValueError):
        raise RuntimeError(
            f"SUMI_LLM_MAX_TOKENS must be an integer in 1..{_LLM_MAX_TOKENS_CEILING}; "
            f"got {raw!r}. Refusing to start on an unparseable cap."
        ) from None
    if not 1 <= value <= _LLM_MAX_TOKENS_CEILING:
        raise RuntimeError(
            f"SUMI_LLM_MAX_TOKENS may only LOWER the {_LLM_MAX_TOKENS_CEILING}-token safety "
            f"ceiling (allowed 1..{_LLM_MAX_TOKENS_CEILING}); got {value}. Refusing to start — "
            f"a cap the environment can raise is not a cap."
        )
    return value


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


def build_llm(*, client=None) -> openai_plugin.LLM:
    """The SINGLE source of truth for Sumi's worker LLM — used by the entrypoint AND
    the safety tests, so the tests exercise the REAL cap (max_completion_tokens =
    _resolve_max_tokens()) rather than a test-local constant. Pass ``client`` (an
    openai.AsyncClient, e.g. one backed by an httpx MockTransport) to drive this exact
    construction path with zero network and no live key.
    """
    max_tokens = _resolve_max_tokens()
    if client is not None:
        return openai_plugin.LLM(
            model=_LLM_MODEL,
            client=client,
            temperature=0.7,
            max_completion_tokens=max_tokens,
            max_retries=0,
        )
    return openai_plugin.LLM(
        model=_LLM_MODEL,
        base_url=_LLM_BASE_URL,
        api_key=_llm_api_key(),
        temperature=0.7,
        max_completion_tokens=max_tokens,
        max_retries=0,
        timeout=httpx.Timeout(30.0),
    )


async def _observe_llm_stream(
    stream: AsyncIterable[livekit_llm.ChatChunk | str | FlushSentinel],
    *,
    max_tokens: int,
) -> AsyncGenerator[livekit_llm.ChatChunk | str | FlushSentinel, None]:
    """Pass through one LLM turn and leave a truthful end-state receipt.

    LiveKit's OpenAI adapter currently discards the provider's literal
    ``finish_reason`` before yielding ``ChatChunk`` objects.  What the worker can
    prove is still enough to classify the first-call anomaly: a normally drained
    provider stream versus a pipeline cancellation/error, together with usage
    and whether the hard cap was reached.
    """
    outcome = "completed"
    completion_tokens: int | None = None
    output_chars = 0
    try:
        async for chunk in stream:
            if isinstance(chunk, str):
                output_chars += len(chunk)
            elif isinstance(chunk, livekit_llm.ChatChunk):
                if chunk.delta and chunk.delta.content:
                    output_chars += len(chunk.delta.content)
                if chunk.usage is not None:
                    completion_tokens = chunk.usage.completion_tokens
            yield chunk
    except asyncio.CancelledError:
        outcome = "cancelled"
        raise
    except GeneratorExit:
        # An upstream node can close an async generator without cancelling the
        # task.  That is still a pipeline-side early stop, not a normally
        # drained provider response.
        outcome = "consumer_closed"
        raise
    except Exception:
        outcome = "error"
        raise
    finally:
        logger.info(
            "sumi llm turn end: outcome=%s completion_tokens=%s output_chars=%d "
            "max_tokens=%d cap_reached=%s provider_finish_reason=unavailable",
            outcome,
            completion_tokens,
            output_chars,
            max_tokens,
            completion_tokens is not None and completion_tokens >= max_tokens,
        )


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
        raise RuntimeError(f"Sumi persona empty: {path}. Refusing to start on a generic fallback.")
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
        # Deterministic fixed opener via session.say() — Sumi's exact first line,
        # synthesized in her own voice, never a generated reply. Her breath is hers.
        await self.session.say(_GREETING)

    async def llm_node(
        self,
        chat_ctx: livekit_llm.ChatContext,
        tools: list[livekit_llm.Tool],
        model_settings: ModelSettings,
    ) -> AsyncIterator[livekit_llm.ChatChunk | str | FlushSentinel]:
        stream = Agent.default.llm_node(self, chat_ctx, tools, model_settings)
        async for chunk in _observe_llm_stream(stream, max_tokens=_resolve_max_tokens()):
            yield chunk

    async def tts_node(
        self,
        text: AsyncIterable[str],
        model_settings: ModelSettings,
    ) -> AsyncIterator[rtc.AudioFrame]:
        """Master Sumi's synthesized PCM using the accepted sample-B curve."""

        stream = Agent.default.tts_node(self, text, model_settings)
        if not _MASTERING_ENABLED:
            async for frame in stream:
                yield frame
            return

        processor = _TelephonyMasteringProcessor()
        async for frame in stream:
            yield _master_audio_frame(frame, processor)


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

    # Slice 3 — LOCAL STT: explicit provider factory. The accepted default is
    # local faster-whisper; Parakeet and sherpa remain opt-in rollback/eval
    # providers. No cloud STT or silent fallback is reachable from this path.
    vad = silero_plugin.VAD.load(
        min_speech_duration=0.1,
        min_silence_duration=0.65,
        prefix_padding_duration=0.4,
    )
    stt = build_stt(vad=vad)
    # Slice 4 — LOCAL LLM: Momo via the explicit LiteLLM `sumi` route, built by the
    # single build_llm() factory — the SAME construction path the safety tests
    # exercise. Its cap is _resolve_max_tokens() (hard 64 ceiling; env may only
    # lower). max_retries=0 so a phone turn is never spoken twice; the route is
    # num_retries:0 with no cloud fallback.
    llm = build_llm()
    # TTS provider is explicit and fail-loud. Voicebook remains the local/R&D
    # path; ElevenLabs is the supported LiveKit streaming control for the phone
    # quality experiment. There is no automatic provider fallback.
    tts = build_tts()

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
        turn_handling=_TURN_HANDLING,
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
    trace(
        f"sumi session: silero-vad -> {_STT_PROVIDER}(local STT) -> "
        f"momo/sumi-route(local LLM) -> {_TTS_PROVIDER}(TTS)"
    )
    trace("sumi: entrypoint complete, greeting scheduled via on_enter")


if __name__ == "__main__":
    cli.run_app(server)
