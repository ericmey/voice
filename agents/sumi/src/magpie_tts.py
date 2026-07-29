"""LiveKit streaming TTS extension for NVIDIA Magpie Zero Shot.

The maintained LiveKit NVIDIA plugin already owns Riva authentication, channel
construction, and service lifecycle, but its TTS call does not expose Riva's
``zero_shot_audio_prompt_file`` or ``zero_shot_quality`` arguments.  This module
keeps that native plugin/client path and adds only the missing Zero Shot request
fields plus Magpie's native 22.05 kHz output contract.
"""

from __future__ import annotations

import asyncio
import queue
import threading
from collections.abc import Mapping
from pathlib import Path
from typing import Any, cast

from livekit.agents import APIConnectOptions, tokenize, tts, utils
from livekit.agents.types import DEFAULT_API_CONNECT_OPTIONS
from livekit.plugins import nvidia as nvidia_plugin

_SAMPLE_RATE = 22050
_NUM_CHANNELS = 1
_MIN_SENTENCE_LEN = 30
_MIN_SYNTH_TEXT_LEN = 80
_MAX_SYNTH_TEXT_LEN = 180
_STREAM_CONTEXT_LEN = 30


class MagpieZeroShotTTS(nvidia_plugin.TTS):
    """The official LiveKit NVIDIA TTS path with Riva Zero Shot conditioning."""

    def __init__(
        self,
        *,
        server: str,
        prompt_path: str | Path,
        quality: int = 40,
        language_code: str = "en-US",
        use_ssl: bool = False,
        api_key: str = "",
        pronunciations: Mapping[str, str] | None = None,
    ) -> None:
        prompt = Path(prompt_path)
        if not prompt.is_file():
            raise ValueError(f"Magpie Zero Shot prompt does not exist: {prompt}")
        if not 1 <= quality <= 40:
            raise ValueError("Magpie Zero Shot quality must be between 1 and 40")

        # ``voice`` is deliberately empty: prompt-conditioned synthesis should
        # not also select one of Magpie's built-in speakers.
        super().__init__(
            server=server,
            voice="",
            function_id="",
            language_code=language_code,
            use_ssl=use_ssl,
            api_key=api_key,
        )
        self._prompt_path = prompt
        self._quality = quality
        self._pronunciations = dict(pronunciations or {})

        # LiveKit's plugin currently fixes TTS output at 16 kHz. Magpie's native
        # output is 22.05 kHz, and retaining it avoids an avoidable pre-phone
        # downsample before LiveKit performs the actual room/telephony resample.
        self._sample_rate = _SAMPLE_RATE
        self._opts.sample_rate = _SAMPLE_RATE
        self._opts.word_tokenizer = tokenize.blingfire.SentenceTokenizer(
            min_sentence_len=_MIN_SENTENCE_LEN,
            min_token_len=_MIN_SYNTH_TEXT_LEN,
            max_token_len=_MAX_SYNTH_TEXT_LEN,
            stream_context_len=_STREAM_CONTEXT_LEN,
            retain_format=True,
        )

    @property
    def model(self) -> str:
        return "magpie-tts-zeroshot"

    @property
    def provider(self) -> str:
        return "nvidia-riva"

    def stream(
        self, *, conn_options: APIConnectOptions = DEFAULT_API_CONNECT_OPTIONS
    ) -> tts.SynthesizeStream:
        return _MagpieZeroShotStream(tts=self, conn_options=conn_options)


class _MagpieZeroShotStream(tts.SynthesizeStream):
    def __init__(
        self,
        *,
        tts: MagpieZeroShotTTS,
        conn_options: APIConnectOptions,
    ) -> None:
        super().__init__(tts=tts, conn_options=conn_options)
        self._tts = tts

    async def _run(self, output_emitter: tts.AudioEmitter) -> None:
        context_id = utils.shortuuid()
        sentence_stream = self._tts._opts.word_tokenizer.stream()
        token_queue: queue.Queue = queue.Queue()
        event_loop = asyncio.get_running_loop()

        output_emitter.initialize(
            request_id=context_id,
            sample_rate=_SAMPLE_RATE,
            num_channels=_NUM_CHANNELS,
            stream=True,
            mime_type="audio/pcm",
        )
        output_emitter.start_segment(segment_id=context_id)
        worker_done = event_loop.create_future()

        async def input_task() -> None:
            async for data in self._input_ch:
                if isinstance(data, self._FlushSentinel):
                    sentence_stream.flush()
                else:
                    sentence_stream.push_text(data)
            sentence_stream.end_input()

        async def segment_task() -> None:
            async for token in sentence_stream:
                token_queue.put(token)
            token_queue.put(None)

        def finish_worker(error: BaseException | None = None) -> None:
            if worker_done.done():
                return
            if error is None:
                worker_done.set_result(None)
            else:
                worker_done.set_exception(error)

        def synthesize_worker() -> None:
            try:
                service = self._tts._ensure_session()
                while True:
                    token = token_queue.get()
                    if token is None:
                        break
                    responses = service.synthesize_online(
                        token.token,
                        voice_name=None,
                        language_code=self._tts._opts.language_code,
                        sample_rate_hz=_SAMPLE_RATE,
                        # nvidia-riva-client 2.26 types this as str but calls
                        # ``.open()`` at runtime. Preserve Path until upstream
                        # makes its annotation and implementation agree.
                        zero_shot_audio_prompt_file=cast(Any, self._tts._prompt_path),
                        zero_shot_quality=self._tts._quality,
                        custom_dictionary=self._tts._pronunciations,
                    )
                    for response in responses:
                        event_loop.call_soon_threadsafe(output_emitter.push, response.audio)
            except BaseException as exc:
                event_loop.call_soon_threadsafe(finish_worker, exc)
            else:
                event_loop.call_soon_threadsafe(finish_worker)

        worker = threading.Thread(
            target=synthesize_worker,
            name="nvidia-magpie-zeroshot-tts",
            daemon=True,
        )
        worker.start()
        tasks = [asyncio.create_task(input_task()), asyncio.create_task(segment_task())]

        try:
            await asyncio.gather(*tasks)
        finally:
            token_queue.put(None)
            await worker_done
            output_emitter.end_segment()
            await sentence_stream.aclose()
