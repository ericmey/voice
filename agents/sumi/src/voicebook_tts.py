"""Slice 5 — Sumi's VOICE: a custom LiveKit TTS plugin over voicebook-stream.

This is Sumi speaking in her own accepted master voice, locally — replacing the
inherited ElevenLabs (Nyla's id) scaffold. It drives the managed voicebook-stream
service's LiveKit-facing endpoint:

    POST {base_url}/speak/stream   body {"voice_id","text"}
      -> raw s16le PCM, 24000 Hz mono (X-Audio-Format: s16le, X-Sample-Rate: 24000)

Contract notes that shaped this adapter (from voicebook-stream QUALIFICATION + app.py):
  - Input is FULL TEXT, not token-streamed, so ``VoicebookTTS`` itself declares
    capabilities.streaming=False. ``build_streaming_voicebook_tts`` wraps it in
    an explicit StreamAdapter whose coalescing policy is part of Sumi's tested
    worker contract (rather than the SDK's per-sentence default).
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
# Raw little-endian s16 PCM — audio/pcm makes AudioEmitter treat bytes as raw PCM
# (no container demux), which is exactly what /speak/stream emits.
_MIME_TYPE = "audio/pcm"

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


def build_streaming_voicebook_tts(
    *,
    voice_id: str,
    base_url: str = "http://voicebook-stream:5060",
    http_session: aiohttp.ClientSession | None = None,
) -> tts.StreamAdapter:
    """Build Sumi's streaming TTS seam with deliberate phrase coalescing.

    ``VoicebookTTS`` accepts full text.  The LiveKit pipeline otherwise wraps it
    in a default sentence adapter that flushes every sentence, including tiny
    fragments.  Constructing the adapter explicitly makes the batching policy a
    tested part of Sumi's worker rather than an implicit SDK default.
    """
    return tts.StreamAdapter(
        tts=VoicebookTTS(
            voice_id=voice_id,
            base_url=base_url,
            http_session=http_session,
        ),
        sentence_tokenizer=tokenize.blingfire.SentenceTokenizer(
            min_sentence_len=_MIN_SENTENCE_LEN,
            min_token_len=_MIN_SYNTH_TEXT_LEN,
            max_token_len=_MAX_SYNTH_TEXT_LEN,
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
        http_session: aiohttp.ClientSession | None = None,
    ) -> None:
        if not voice_id:
            raise ValueError("VoicebookTTS requires a voice_id — Sumi does not speak anonymously.")
        super().__init__(
            capabilities=tts.TTSCapabilities(streaming=False),
            sample_rate=_SAMPLE_RATE,
            num_channels=_NUM_CHANNELS,
        )
        self._voice_id = voice_id
        self._base_url = base_url.rstrip("/")
        self._session = http_session

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
        try:
            async with self._tts._ensure_session().post(
                f"{self._tts._base_url}/speak/stream",
                json={"voice_id": self._tts._voice_id, "text": self._input_text},
                timeout=aiohttp.ClientTimeout(
                    total=None,  # streamed audio: bounded by sock_connect, not total
                    sock_connect=self._conn_options.timeout,
                ),
            ) as resp:
                resp.raise_for_status()

                # Defence-in-depth: a 200 must be the raw-PCM contract, not some
                # other body. Verify the instrument rather than trust the status.
                fmt = resp.headers.get("X-Audio-Format", "")
                if fmt and fmt.lower() != "s16le":
                    body = await resp.text()
                    raise APIError(
                        message=f"voicebook-stream returned X-Audio-Format={fmt!r}, not s16le",
                        body=body,
                    )

                output_emitter.initialize(
                    request_id=utils.shortuuid(),
                    sample_rate=_SAMPLE_RATE,
                    num_channels=_NUM_CHANNELS,
                    mime_type=_MIME_TYPE,
                )
                async for data, _ in resp.content.iter_chunks():
                    output_emitter.push(data)
                output_emitter.flush()

        except TimeoutError as e:
            raise APITimeoutError() from e
        except aiohttp.ClientResponseError as e:
            raise APIStatusError(
                message=e.message,
                status_code=e.status,
                request_id=None,
                body=None,
            ) from e
        except APIError:
            # Our own format-guard (and any APIStatusError): keep the status/meaning,
            # don't collapse it into a generic connection error.
            raise
        except Exception as e:
            raise APIConnectionError() from e
