"""Slice 5 — Sumi's VOICE: a custom LiveKit TTS plugin over voicebook-stream.

This is Sumi speaking in her own accepted master voice, locally — replacing the
inherited ElevenLabs (Nyla's id) scaffold. It drives the managed voicebook-stream
service's LiveKit-facing endpoint:

    POST {base_url}/speak/stream   body {"voice_id","text","response_format"}
      -> progressive PCM or WAV, 24000 Hz mono

Contract notes that shaped this adapter (from voicebook-stream QUALIFICATION + app.py):
  - Input is FULL TEXT, not token-streamed, so ``VoicebookTTS`` itself declares
    capabilities.streaming=False. ``build_streaming_voicebook_tts`` wraps it in
    an explicit StreamAdapter. The production coalescing policy stays the
    default; an explicit ``whole_reply`` diagnostic mode buffers until end-input
    so one LLM turn becomes one synthesis request.
  - The service holds a ONE-FLIGHT lease: a second concurrent synthesis gets 429.
    For a single Sumi call that is fine; a 429 is a transient, retryable state.
  - Cancellation is safe: LiveKit cancels the _run task, aiohttp closes the
    connection, and the server observes the disconnect and releases the lease
    (voicebook-stream T6 qual). No explicit abort handshake is needed here.
  - TTS retries do NOT double-speak. Unlike the LLM layer (which re-emits already-
    streamed tokens on retry — why Sumi's LLM is pinned to max_retry=0), the TTS
    base _main_task calls output_emitter.aclose() to DISCARD a failed attempt's
    audio before retrying under a fresh request_id. So the default TTS retry is
    safe and we do not force it to 0.

No secret is handled here: voicebook-stream is an internal service with no api-key
on the stream path; the voice_id selects Sumi's frozen master voice server-side.
"""

from __future__ import annotations

import asyncio
import itertools
import json
import logging
import time
from dataclasses import dataclass
from pathlib import Path

import aiohttp
from livekit.agents import (
    APIConnectionError,
    APIConnectOptions,
    APIError,
    APIStatusError,
    APITimeoutError,
    tokenize,
    tts,
    utils,
)
from livekit.agents.types import DEFAULT_API_CONNECT_OPTIONS

# voicebook-stream's fixed output contract (synth.SAMPLE_RATE / CHANNELS).
_SAMPLE_RATE = 24000
_NUM_CHANNELS = 1
_WIRE_FORMATS = {"pcm", "wav"}

# The default LiveKit sentence adapter emits every sentence independently.  On
# Sumi's first real call that turned 373 characters into 11 voicebook requests,
# including several 12--30 character clips.  Every sample arrived over RTP, but
# the independent clips reset prosody and made their joins audible.  Batch up to
# a natural phone-turn-sized phrase before synthesizing while keeping a hard
# upper bound so first audio does not wait for a whole monologue.
_MIN_SYNTH_TEXT_LEN = 80
_MAX_SYNTH_TEXT_LEN = 180
_MIN_SENTENCE_LEN = 30
_STREAM_CONTEXT_LEN = 30
_WHOLE_REPLY_TEXT_LEN = 4000  # voicebook-stream's request ceiling
_TEXT_MODES = {"coalesced", "whole_reply"}

logger = logging.getLogger("voice.agent")


@dataclass(frozen=True)
class _SourceCapturePaths:
    sequence: int
    partial: Path
    final: Path
    manifest: Path


def build_streaming_voicebook_tts(
    *,
    voice_id: str,
    base_url: str = "http://voicebook-stream:5060",
    wire_format: str = "pcm",
    text_mode: str = "coalesced",
    capture_dir: Path | None = None,
    capture_call_sid: str | None = None,
    http_session: aiohttp.ClientSession | None = None,
) -> tts.StreamAdapter:
    """Build Sumi's streaming TTS seam with deliberate phrase coalescing.

    ``VoicebookTTS`` accepts full text. The LiveKit pipeline otherwise wraps it
    in a default sentence adapter that flushes every sentence, including tiny
    fragments. Constructing the adapter explicitly makes the batching policy a
    tested part of Sumi's worker rather than an implicit SDK default.

    ``coalesced`` preserves the qualified 80--180 character streaming policy.
    ``whole_reply`` holds incremental LLM text until end-input (bounded by the
    service's 4000-character request limit) and is intentionally opt-in because
    it trades first-audio latency for acoustic continuity.
    """
    text_mode = text_mode.strip().lower()
    if text_mode not in _TEXT_MODES:
        raise ValueError(
            f"Voicebook text_mode must be one of {sorted(_TEXT_MODES)}, got {text_mode!r}"
        )
    min_text_len = (
        _WHOLE_REPLY_TEXT_LEN if text_mode == "whole_reply" else _MIN_SYNTH_TEXT_LEN
    )
    max_text_len = (
        _WHOLE_REPLY_TEXT_LEN if text_mode == "whole_reply" else _MAX_SYNTH_TEXT_LEN
    )
    return tts.StreamAdapter(
        tts=VoicebookTTS(
            voice_id=voice_id,
            base_url=base_url,
            wire_format=wire_format,
            capture_dir=capture_dir,
            capture_call_sid=capture_call_sid,
            http_session=http_session,
        ),
        sentence_tokenizer=tokenize.blingfire.SentenceTokenizer(
            min_sentence_len=_MIN_SENTENCE_LEN,
            min_token_len=min_text_len,
            max_token_len=max_text_len,
            stream_context_len=_STREAM_CONTEXT_LEN,
            retain_format=True,
        ),
    )


class VoicebookTTS(tts.TTS):
    """LiveKit TTS backed by the managed voicebook-stream service.

    ``voice_id`` selects Sumi's frozen master voice from the server-owned registry;
    an unknown id fails loud (404 -> APIStatusError), never a substitute voice.
    """

    def __init__(
        self,
        *,
        voice_id: str,
        base_url: str = "http://voicebook-stream:5060",
        wire_format: str = "pcm",
        capture_dir: Path | None = None,
        capture_call_sid: str | None = None,
        http_session: aiohttp.ClientSession | None = None,
    ) -> None:
        if not voice_id:
            raise ValueError("VoicebookTTS requires a voice_id — Sumi does not speak anonymously.")
        wire_format = wire_format.strip().lower()
        if wire_format not in _WIRE_FORMATS:
            raise ValueError(
                f"VoicebookTTS wire_format must be one of {sorted(_WIRE_FORMATS)}, got {wire_format!r}"
            )
        super().__init__(
            capabilities=tts.TTSCapabilities(streaming=False),
            sample_rate=_SAMPLE_RATE,
            num_channels=_NUM_CHANNELS,
        )
        self._voice_id = voice_id
        self._base_url = base_url.rstrip("/")
        self._wire_format = wire_format
        self._session = http_session
        self._capture_dir = capture_dir
        self._capture_call_sid = capture_call_sid
        self._capture_sequence = itertools.count()
        if (capture_dir is None) != (capture_call_sid is None):
            raise ValueError("Voicebook raw capture requires both capture_dir and capture_call_sid")

    def _next_capture_paths(self) -> _SourceCapturePaths | None:
        if self._capture_dir is None or self._capture_call_sid is None:
            return None
        sequence = next(self._capture_sequence)
        request_tag = utils.shortuuid()
        extension = "wav" if self._wire_format == "wav" else "s16le"
        stem = f"{self._capture_call_sid}.voicebook.{sequence:03d}-{request_tag}"
        final_path = self._capture_dir / f"{stem}.{extension}"
        return _SourceCapturePaths(
            sequence=sequence,
            partial=final_path.with_suffix(final_path.suffix + ".partial"),
            final=final_path,
            manifest=Path(f"{final_path}.json"),
        )

    def _ensure_session(self) -> aiohttp.ClientSession:
        if not self._session:
            self._session = utils.http_context.http_session()
        return self._session

    def synthesize(
        self,
        text: str,
        *,
        conn_options: APIConnectOptions = DEFAULT_API_CONNECT_OPTIONS,
    ) -> ChunkedStream:
        return ChunkedStream(tts=self, input_text=text, conn_options=conn_options)


class ChunkedStream(tts.ChunkedStream):
    """One /speak/stream request -> pushed s16le PCM frames."""

    def __init__(
        self,
        *,
        tts: VoicebookTTS,
        input_text: str,
        conn_options: APIConnectOptions,
    ) -> None:
        super().__init__(tts=tts, input_text=input_text, conn_options=conn_options)
        self._tts: VoicebookTTS = tts

    async def _run(self, output_emitter: tts.AudioEmitter) -> None:
        capture_paths = self._tts._next_capture_paths()
        capture_file = None
        capture_bytes = 0
        capture_chunks = 0
        capture_started = time.time()
        capture_outcome = "failed"
        capture_error: str | None = None
        if capture_paths is not None:
            try:
                capture_paths.partial.parent.mkdir(parents=True, exist_ok=True)
                capture_file = capture_paths.partial.open("xb")
            except OSError as exc:
                capture_error = f"{type(exc).__name__}: {exc}"
                logger.warning(
                    "voicebook source capture open failed call_sid=%s: %s",
                    self._tts._capture_call_sid,
                    exc,
                )
        try:
            request = {"voice_id": self._tts._voice_id, "text": self._input_text}
            if self._tts._wire_format == "wav":
                request["response_format"] = "wav"

            async with self._tts._ensure_session().post(
                f"{self._tts._base_url}/speak/stream",
                json=request,
                timeout=aiohttp.ClientTimeout(
                    total=None,  # streamed audio: bounded by sock_connect, not total
                    sock_connect=self._conn_options.timeout,
                ),
            ) as resp:
                resp.raise_for_status()

                # Defence-in-depth: a 200 must be the raw-PCM contract, not some
                # other body. Verify the instrument rather than trust the status.
                expected_format = "wav" if self._tts._wire_format == "wav" else "s16le"
                fmt = resp.headers.get("X-Audio-Format", "")
                if fmt and fmt.lower() != expected_format:
                    body = await resp.text()
                    raise APIError(
                        message=(
                            f"voicebook-stream returned X-Audio-Format={fmt!r}, "
                            f"not {expected_format}"
                        ),
                        body=body,
                    )

                output_emitter.initialize(
                    request_id=utils.shortuuid(),
                    sample_rate=_SAMPLE_RATE,
                    num_channels=_NUM_CHANNELS,
                    mime_type=(
                        "audio/wav" if self._tts._wire_format == "wav" else "audio/pcm"
                    ),
                )
                async for data, _ in resp.content.iter_chunks():
                    if capture_file is not None:
                        try:
                            capture_file.write(data)
                            capture_bytes += len(data)
                            capture_chunks += 1
                        except OSError as exc:
                            capture_error = f"{type(exc).__name__}: {exc}"
                            logger.warning(
                                "voicebook source capture write failed call_sid=%s: %s",
                                self._tts._capture_call_sid,
                                exc,
                            )
                            capture_file.close()
                            capture_file = None
                    output_emitter.push(data)
                output_emitter.flush()
                capture_outcome = "completed"

        except TimeoutError as e:
            capture_error = capture_error or f"{type(e).__name__}: {e}"
            raise APITimeoutError() from e
        except aiohttp.ClientResponseError as e:
            capture_error = capture_error or f"HTTP {e.status}: {e.message}"
            raise APIStatusError(
                message=e.message,
                status_code=e.status,
                request_id=None,
                body=None,
            ) from e
        except APIError as e:
            # Our own format-guard (and any APIStatusError): keep the status/meaning,
            # don't collapse it into a generic connection error.
            capture_error = capture_error or f"{type(e).__name__}: {e}"
            raise
        except asyncio.CancelledError as e:
            capture_outcome = "cancelled"
            capture_error = capture_error or type(e).__name__
            raise
        except Exception as e:
            capture_error = capture_error or f"{type(e).__name__}: {e}"
            raise APIConnectionError() from e
        finally:
            if capture_file is not None:
                try:
                    capture_file.flush()
                    capture_file.close()
                except OSError as exc:
                    capture_error = capture_error or f"{type(exc).__name__}: {exc}"
            if capture_paths is not None:
                if capture_outcome == "completed" and capture_error is None:
                    try:
                        capture_paths.partial.replace(capture_paths.final)
                    except OSError as exc:
                        capture_error = f"{type(exc).__name__}: {exc}"
                manifest = {
                    "schema": "voicebook-source-capture/v1",
                    "call_sid": self._tts._capture_call_sid,
                    "sequence": capture_paths.sequence,
                    "wire_format": self._tts._wire_format,
                    "text": self._input_text,
                    "started_at": capture_started,
                    "completed_at": time.time(),
                    "outcome": capture_outcome if capture_error is None else "capture_failed",
                    "bytes": capture_bytes,
                    "chunks": capture_chunks,
                    "path": str(
                        capture_paths.final
                        if capture_paths.final.exists()
                        else capture_paths.partial
                    ),
                    "error": capture_error,
                }
                try:
                    capture_paths.manifest.write_text(json.dumps(manifest, indent=2) + "\n")
                except OSError as exc:
                    logger.warning(
                        "voicebook source capture manifest failed call_sid=%s: %s",
                        self._tts._capture_call_sid,
                        exc,
                    )
                logger.info(
                    "voicebook source capture: call_sid=%s outcome=%s path=%s bytes=%d chunks=%d",
                    self._tts._capture_call_sid,
                    manifest["outcome"],
                    manifest["path"],
                    capture_bytes,
                    capture_chunks,
                )
