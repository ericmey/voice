"""LiveKit streaming STT adapter for sherpa-onnx's websocket server.

The server protocol is deliberately small and comes from sherpa-onnx's own
``online-websocket-client-decode-file.py`` example:

* binary messages are little-endian float32 mono samples at 16 kHz;
* the text message ``Done`` flushes the current stream;
* JSON text messages carry interim/final recognition results; and
* ``Done!`` closes the flushed stream.

Keeping sherpa-onnx in a separate service avoids adding its CUDA/ONNX runtime
to the voice-agent image.  It also makes the STT provider a reversible runtime
choice rather than an agent rebuild dependency.
"""

from __future__ import annotations

import asyncio
import json
import sys
import time
from array import array
from collections.abc import Iterable
from typing import Any

import aiohttp
from livekit import rtc
from livekit.agents import (
    DEFAULT_API_CONNECT_OPTIONS,
    APIConnectionError,
    APIConnectOptions,
    LanguageCode,
    stt,
    utils,
)
from livekit.agents.types import NOT_GIVEN, NotGivenOr
from livekit.agents.utils import AudioBuffer, is_given


def _frame_as_float32_bytes(frame: rtc.AudioFrame) -> bytes:
    """Convert LiveKit signed-16 PCM to sherpa's normalized float32 wire format."""

    if frame.num_channels != 1:
        raise ValueError(f"sherpa STT requires mono audio, got {frame.num_channels} channels")

    pcm = array("h")
    pcm.frombytes(frame.data.tobytes())
    if sys.byteorder != "little":
        pcm.byteswap()
    samples = array("f", (sample / 32768.0 for sample in pcm))
    if sys.byteorder != "little":
        samples.byteswap()
    return samples.tobytes()


def _result_event_type(result: dict[str, Any]) -> stt.SpeechEventType:
    return (
        stt.SpeechEventType.FINAL_TRANSCRIPT
        if result.get("is_final") or result.get("is_eof")
        else stt.SpeechEventType.INTERIM_TRANSCRIPT
    )


class SherpaSTT(stt.STT):
    """True-streaming sherpa-onnx STT reached over its maintained websocket API."""

    def __init__(
        self,
        *,
        url: str = "ws://sherpa-stt:6006",
        model: str = "nemotron-speech-streaming-en-0.6b-560ms-int8",
        language: str = "en-US",
        sample_rate: int = 16000,
    ) -> None:
        super().__init__(
            capabilities=stt.STTCapabilities(
                streaming=True,
                interim_results=True,
                aligned_transcript=False,
                offline_recognize=True,
            )
        )
        self._url = url
        self._model = model
        self._language = LanguageCode(language)
        self._sample_rate = sample_rate

    @property
    def model(self) -> str:
        return self._model

    @property
    def provider(self) -> str:
        return "sherpa-onnx"

    def stream(
        self,
        *,
        language: NotGivenOr[str] = NOT_GIVEN,
        conn_options: APIConnectOptions = DEFAULT_API_CONNECT_OPTIONS,
    ) -> SherpaSpeechStream:
        effective_language = self._language if not is_given(language) else LanguageCode(language)
        return SherpaSpeechStream(
            stt_instance=self,
            conn_options=conn_options,
            language=effective_language,
        )

    async def _recognize_impl(
        self,
        buffer: AudioBuffer,
        *,
        language: NotGivenOr[str] = NOT_GIVEN,
        conn_options: APIConnectOptions,
    ) -> stt.SpeechEvent:
        stream = self.stream(language=language, conn_options=conn_options)
        frames: Iterable[rtc.AudioFrame]
        if isinstance(buffer, rtc.AudioFrame):
            frames = (buffer,)
        else:
            frames = buffer
        async with stream:
            for frame in frames:
                stream.push_frame(frame)
            stream.end_input()
            final: stt.SpeechEvent | None = None
            async for event in stream:
                if event.type == stt.SpeechEventType.FINAL_TRANSCRIPT:
                    final = event
            if final is None:
                raise APIConnectionError("sherpa-onnx returned no final transcript")
            return final


class SherpaSpeechStream(stt.SpeechStream):
    def __init__(
        self,
        *,
        stt_instance: SherpaSTT,
        conn_options: APIConnectOptions,
        language: LanguageCode,
    ) -> None:
        super().__init__(
            stt=stt_instance,
            conn_options=conn_options,
            sample_rate=stt_instance._sample_rate,
        )
        self._sherpa = stt_instance
        self._language = language
        self._speaking = False
        self._request_id = ""

    async def _run(self) -> None:
        # Positional construction keeps compatibility with aiohttp's published
        # runtime signature and the older type stub shipped by this version.
        timeout = aiohttp.ClientWSTimeout(None, 5.0)  # type: ignore[call-arg]
        session = utils.http_context.http_session()
        try:
            async with session.ws_connect(self._sherpa._url, timeout=timeout) as ws:
                sender = asyncio.create_task(self._send_audio(ws), name="sherpa-stt-send")
                try:
                    await self._receive_results(ws)
                    await sender
                finally:
                    if not sender.done():
                        sender.cancel()
                        await asyncio.gather(sender, return_exceptions=True)
        except (aiohttp.ClientError, TimeoutError) as exc:
            raise APIConnectionError(f"sherpa-onnx websocket failed: {exc}") from exc

    async def _send_audio(self, ws: aiohttp.ClientWebSocketResponse) -> None:
        async for item in self._input_ch:
            if isinstance(item, rtc.AudioFrame):
                payload = _frame_as_float32_bytes(item)
                if payload:
                    await ws.send_bytes(payload)
            elif isinstance(item, self._FlushSentinel):
                await ws.send_str("Done")
                return

    async def _receive_results(self, ws: aiohttp.ClientWebSocketResponse) -> None:
        async for message in ws:
            if message.type == aiohttp.WSMsgType.TEXT:
                if message.data == "Done!":
                    return
                self._handle_result(json.loads(message.data))
            elif message.type == aiohttp.WSMsgType.ERROR:
                raise APIConnectionError(f"sherpa-onnx websocket error: {ws.exception()}")
            elif message.type in {aiohttp.WSMsgType.CLOSE, aiohttp.WSMsgType.CLOSED}:
                return

    def _handle_result(self, result: dict[str, Any]) -> None:
        text = str(result.get("text", "")).strip()
        if not text:
            return

        if not self._speaking:
            self._speaking = True
            self._event_ch.send_nowait(stt.SpeechEvent(type=stt.SpeechEventType.START_OF_SPEECH))

        segment = result.get("segment", 0)
        self._request_id = f"sherpa-{id(self)}-{segment}"
        event_type = _result_event_type(result)
        timestamps = result.get("timestamps") or []
        start_time = float(result.get("start_time", 0.0)) + self.start_time_offset
        end_time = start_time + (float(timestamps[-1]) if timestamps else 0.0)
        self._event_ch.send_nowait(
            stt.SpeechEvent(
                type=event_type,
                request_id=self._request_id,
                alternatives=[
                    stt.SpeechData(
                        language=self._language,
                        start_time=start_time,
                        end_time=end_time,
                        text=text,
                    )
                ],
            )
        )

        if event_type == stt.SpeechEventType.FINAL_TRANSCRIPT:
            if self._speaking:
                self._event_ch.send_nowait(stt.SpeechEvent(type=stt.SpeechEventType.END_OF_SPEECH))
            self._speaking = False
            self._event_ch.send_nowait(
                stt.SpeechEvent(
                    type=stt.SpeechEventType.RECOGNITION_USAGE,
                    request_id=self._request_id,
                    recognition_usage=stt.RecognitionUsage(
                        audio_duration=max(0.0, time.time() - self.start_time)
                    ),
                )
            )
