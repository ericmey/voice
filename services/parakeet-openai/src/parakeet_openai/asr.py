"""Riva streaming ASR client -- the only recognition contract our profile serves.

Our deployed NIM profile is `mode=str` (streaming). Its own HTTP
/v1/audio/transcriptions route exists but returns "Model not found for
language" because no offline model is registered. gRPC streaming is what works,
and it works well: measured 64 concurrent streams, zero errors, 93x realtime.

This module is the ONLY place that talks gRPC. The HTTP layer above it stays
ignorant of Riva entirely.
"""

from __future__ import annotations

import os
from dataclasses import dataclass

TARGET_RATE = 16000
# Chunk the PCM the way a live microphone would. Riva's streaming recognizer is
# built for real-time arrival; feeding one enormous buffer is legal but pushes
# the whole file through a path tuned for increments.
CHUNK_BYTES = TARGET_RATE * 2 // 10  # 100 ms of s16 mono


class AsrError(RuntimeError):
    """Recognition failed. Surfaced to the caller, never turned into empty text."""


@dataclass(frozen=True)
class Transcript:
    text: str
    duration_s: float


class RivaTranscriber:
    """Thin wrapper over riva.client streaming recognition.

    The riva import happens at construction, not module import, so the HTTP app
    and its tests load without the SDK present.
    """

    def __init__(
        self,
        uri: str | None = None,
        language: str | None = None,
        punctuate: bool | None = None,
    ) -> None:
        self.uri = uri or os.environ.get("PARAKEET_URI", "parakeet-ctl:50051")
        self.language = language or os.environ.get("PARAKEET_LANGUAGE", "en-US")
        self.punctuate = (
            punctuate
            if punctuate is not None
            else os.environ.get("PARAKEET_PUNCTUATE", "true").lower() == "true"
        )
        try:
            import riva.client  # noqa: PLC0415 - deliberate lazy import
        except ImportError as exc:  # pragma: no cover - environment-dependent
            raise AsrError(
                "nvidia-riva-client is not installed; the transcription shim "
                "cannot reach Parakeet without it"
            ) from exc
        self._riva = riva.client
        self._auth = riva.client.Auth(uri=self.uri, use_ssl=False)
        self._asr = riva.client.ASRService(self._auth)

    def _config(self):
        cfg = self._riva.RecognitionConfig(
            encoding=self._riva.AudioEncoding.LINEAR_PCM,
            language_code=self.language,
            sample_rate_hertz=TARGET_RATE,
            max_alternatives=1,
            enable_automatic_punctuation=self.punctuate,
            audio_channel_count=1,
        )
        return self._riva.StreamingRecognitionConfig(config=cfg, interim_results=False)

    def transcribe(self, pcm: bytes) -> Transcript:
        """Stream PCM through Riva and return the concatenated FINAL transcript.

        Only `is_final` results are joined. Interim results are partial by
        definition -- accumulating them produces duplicated, truncated text that
        reads as a plausible transcript and is not one.
        """
        if not pcm:
            raise AsrError("no audio to transcribe")

        def chunks():
            for i in range(0, len(pcm), CHUNK_BYTES):
                yield pcm[i : i + CHUNK_BYTES]

        parts: list[str] = []
        try:
            responses = self._asr.streaming_response_generator(
                audio_chunks=chunks(), streaming_config=self._config()
            )
            for response in responses:
                for result in response.results:
                    if not result.is_final or not result.alternatives:
                        continue
                    text = result.alternatives[0].transcript.strip()
                    if text:
                        parts.append(text)
        except Exception as exc:  # noqa: BLE001 - normalize to one error type
            raise AsrError(f"{type(exc).__name__}: {exc}") from exc

        return Transcript(
            text=" ".join(parts).strip(),
            duration_s=len(pcm) / (TARGET_RATE * 2),
        )
