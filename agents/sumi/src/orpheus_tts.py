"""Orpheus TTS adapter for LiveKit Agents.

Orpheus-FastAPI exposes an OpenAI-shaped ``/v1/audio/speech`` endpoint, but
it is *not* streaming-compatible: it ignores ``response_format`` and always
returns a complete WAV container. The stock ``openai.TTS`` plugin can't
consume that (it pushes no audio frames → dead air), so this purpose-built
adapter does the honest thing:

  1. POST the text to ``/v1/audio/speech`` and read the full WAV response.
  2. Decode the WAV to raw PCM ourselves (we know it's 24 kHz mono s16le).
  3. Push it to the AudioEmitter as ``audio/pcm`` — raw, no container decode.

Sumi's voice runs on this. When we clone her real voice the endpoint stays
the same; only the ``voice`` id changes.
"""

from __future__ import annotations

import io
import wave

import aiohttp
from livekit.agents import (
    DEFAULT_API_CONNECT_OPTIONS,
    APIConnectOptions,
    tts,
    utils,
)

_DEFAULT_SAMPLE_RATE = 24000  # Orpheus SNAC decoder output rate


class OrpheusTTS(tts.TTS):
    """Chained (non-streaming) TTS backed by an Orpheus-FastAPI server."""

    def __init__(
        self,
        *,
        base_url: str,
        voice: str = "tara",
        model: str = "orpheus",
        sample_rate: int = _DEFAULT_SAMPLE_RATE,
        http_session: aiohttp.ClientSession | None = None,
    ) -> None:
        super().__init__(
            capabilities=tts.TTSCapabilities(streaming=False),
            sample_rate=sample_rate,
            num_channels=1,
        )
        # Normalize so both ".../v1" and ".../" bases resolve to /v1/audio/speech.
        self._speech_url = base_url.rstrip("/")
        if not self._speech_url.endswith("/v1"):
            self._speech_url += "/v1"
        self._speech_url += "/audio/speech"
        self._voice = voice
        self._model = model
        self._session = http_session

    def _ensure_session(self) -> aiohttp.ClientSession:
        if self._session is None:
            self._session = utils.http_context.http_session()
        return self._session

    def synthesize(
        self,
        text: str,
        *,
        conn_options: APIConnectOptions = DEFAULT_API_CONNECT_OPTIONS,
    ) -> OrpheusChunkedStream:
        return OrpheusChunkedStream(tts=self, input_text=text, conn_options=conn_options)


class OrpheusChunkedStream(tts.ChunkedStream):
    def __init__(
        self, *, tts: OrpheusTTS, input_text: str, conn_options: APIConnectOptions
    ) -> None:
        super().__init__(tts=tts, input_text=input_text, conn_options=conn_options)
        self._tts: OrpheusTTS = tts

    async def _run(self, output_emitter: tts.AudioEmitter) -> None:
        session = self._tts._ensure_session()
        payload = {
            "model": self._tts._model,
            "voice": self._tts._voice,
            "input": self._input_text,
            "response_format": "wav",
        }
        timeout = aiohttp.ClientTimeout(total=self._conn_options.timeout)
        async with session.post(self._tts._speech_url, json=payload, timeout=timeout) as resp:
            resp.raise_for_status()
            wav_bytes = await resp.read()

        with wave.open(io.BytesIO(wav_bytes), "rb") as wf:
            sample_rate = wf.getframerate()
            num_channels = wf.getnchannels()
            pcm = wf.readframes(wf.getnframes())

        output_emitter.initialize(
            request_id=utils.shortuuid(),
            sample_rate=sample_rate,
            num_channels=num_channels,
            mime_type="audio/pcm",
        )
        output_emitter.push(pcm)
        output_emitter.flush()
