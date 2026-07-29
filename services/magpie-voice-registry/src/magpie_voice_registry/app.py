from __future__ import annotations

import asyncio
import io
import logging
import time
import uuid
import wave
from collections.abc import AsyncIterator

import httpx
from fastapi import FastAPI, Header, HTTPException, Response
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field

from .aliases import SpeechAliases, empty_speech_aliases
from .pronunciation import PronunciationDictionary, empty_pronunciations
from .registry import UnknownVoice, VoiceRegistry, VoiceSpec

LOG = logging.getLogger("magpie_voice_registry")
MAX_INPUT_CHARS = 2000


class SpeakRequest(BaseModel):
    voice_id: str = Field(min_length=1)
    text: str = Field(min_length=1, max_length=MAX_INPUT_CHARS)


class OpenAISpeechRequest(BaseModel):
    model: str = "magpie-tts-zeroshot"
    input: str = Field(min_length=1, max_length=MAX_INPUT_CHARS)
    voice: str = Field(min_length=1)
    response_format: str = "wav"
    speed: float = 1.0


def _wav(payload: bytes, sample_rate: int) -> bytes:
    output = io.BytesIO()
    with wave.open(output, "wb") as wav:
        wav.setnchannels(1)
        wav.setsampwidth(2)
        wav.setframerate(sample_rate)
        wav.writeframes(payload)
    return output.getvalue()


def create_app(
    registry: VoiceRegistry,
    nim_url: str,
    *,
    capacity: int = 8,
    pronunciations: PronunciationDictionary | None = None,
    speech_aliases: SpeechAliases | None = None,
    client_factory=httpx.AsyncClient,
) -> FastAPI:
    app = FastAPI(title="Magpie named voice registry", version="0.1.0")
    semaphore = asyncio.Semaphore(capacity)
    endpoint = f"{nim_url.rstrip('/')}/v1/audio/synthesize_online"
    pronunciation_dictionary = pronunciations or empty_pronunciations()
    aliases = speech_aliases or empty_speech_aliases()

    def resolve(voice_id: str) -> VoiceSpec:
        try:
            return registry.get(voice_id)
        except UnknownVoice:
            raise HTTPException(
                status_code=404,
                detail=f"unknown voice_id {voice_id!r}; allowed: {registry.voice_ids}",
            ) from None

    async def acquire() -> None:
        try:
            await asyncio.wait_for(semaphore.acquire(), timeout=0.01)
        except TimeoutError:
            raise HTTPException(status_code=429, detail=f"Magpie capacity is {capacity}") from None

    async def synthesize(spec: VoiceSpec, text: str, request_id: str) -> AsyncIterator[bytes]:
        """Start NIM and read its first audio chunk before HTTP response headers.

        FastAPI commits a StreamingResponse's 200 status before it advances the
        body iterator. Starting NIM inside that iterator would turn an upstream
        4xx/5xx into a misleading downstream 200 whose body merely explodes.
        Prefetching one chunk here keeps the fail-loud contract truthful while
        preserving progressive delivery for the remainder.
        """
        await acquire()
        started = time.monotonic()
        outcome = "cancelled"
        client = client_factory(timeout=httpx.Timeout(300.0, connect=10.0))
        response: httpx.Response | None = None
        try:
            with spec.prompt_path.open("rb") as prompt:
                request = client.build_request(
                    "POST",
                    endpoint,
                    data={
                        "text": aliases.apply(text),
                        "language": spec.language,
                        "sample_rate_hz": str(spec.sample_rate),
                        "encoding": "LINEAR_PCM",
                        "prompt_quality": str(spec.quality),
                        "custom_dictionary": pronunciation_dictionary.riva_value,
                    },
                    files={"audio_prompt": (spec.prompt_path.name, prompt, "audio/wav")},
                )
                response = await client.send(request, stream=True)
            if response.status_code != 200:
                detail = (await response.aread())[:500].decode("utf-8", "replace")
                outcome = f"nim_{response.status_code}"
                raise HTTPException(
                    status_code=502,
                    detail=f"Magpie NIM returned HTTP {response.status_code}: {detail}",
                )
            iterator = response.aiter_bytes()
            try:
                first = await anext(iterator)
            except StopAsyncIteration:
                outcome = "empty"
                raise HTTPException(
                    status_code=502, detail="Magpie NIM returned empty audio"
                ) from None

            async def chunks() -> AsyncIterator[bytes]:
                nonlocal outcome
                try:
                    yield first
                    async for chunk in iterator:
                        if chunk:
                            yield chunk
                    outcome = "ok"
                finally:
                    await response.aclose()
                    await client.aclose()
                    semaphore.release()
                    LOG.info(
                        "synthesis request_id=%s voice_id=%s chars=%d quality=%d "
                        "outcome=%s duration_ms=%d",
                        request_id,
                        spec.voice_id,
                        len(text),
                        spec.quality,
                        outcome,
                        round((time.monotonic() - started) * 1000),
                    )

            return chunks()
        except httpx.HTTPError as exc:
            outcome = "unreachable"
            if response is not None:
                await response.aclose()
            await client.aclose()
            semaphore.release()
            LOG.info(
                "synthesis request_id=%s voice_id=%s chars=%d quality=%d outcome=%s duration_ms=%d",
                request_id,
                spec.voice_id,
                len(text),
                spec.quality,
                outcome,
                round((time.monotonic() - started) * 1000),
            )
            raise HTTPException(status_code=502, detail=f"Magpie NIM unavailable: {exc}") from exc
        except BaseException:
            if response is not None:
                await response.aclose()
            await client.aclose()
            semaphore.release()
            LOG.info(
                "synthesis request_id=%s voice_id=%s chars=%d quality=%d outcome=%s duration_ms=%d",
                request_id,
                spec.voice_id,
                len(text),
                spec.quality,
                outcome,
                round((time.monotonic() - started) * 1000),
            )
            raise

    @app.get("/healthz")
    async def health() -> dict:
        ready = False
        backend = "unreachable"
        try:
            async with client_factory(timeout=5.0) as client:
                response = await client.get(f"{nim_url.rstrip('/')}/v1/health/ready")
                ready = response.status_code == 200
                backend = "ready" if ready else f"http_{response.status_code}"
        except httpx.HTTPError:
            pass
        return {
            "ready": ready,
            "backend": f"magpie-nim:{backend}",
            "voices": registry.voice_ids,
            "sample_rate": 22050,
            "max_input_chars": MAX_INPUT_CHARS,
            "capacity": capacity,
            "pronunciations": pronunciation_dictionary.count,
            "pronunciation_sha256": pronunciation_dictionary.sha256,
            "speech_aliases": aliases.count,
            "speech_aliases_sha256": aliases.sha256,
        }

    @app.get("/voices")
    async def voices() -> dict:
        return {
            "voices": [
                {
                    "voice_id": voice_id,
                    "quality": registry.get(voice_id).quality,
                    "language": registry.get(voice_id).language,
                    "sample_rate": registry.get(voice_id).sample_rate,
                }
                for voice_id in registry.voice_ids
            ]
        }

    @app.post("/speak/stream")
    async def speak_stream(
        request: SpeakRequest, x_request_id: str | None = Header(default=None)
    ) -> StreamingResponse:
        spec = resolve(request.voice_id)
        request_id = x_request_id or uuid.uuid4().hex[:12]
        return StreamingResponse(
            await synthesize(spec, request.text.strip(), request_id),
            media_type="application/octet-stream",
            headers={
                "X-Request-ID": request_id,
                "X-Audio-Sample-Rate": str(spec.sample_rate),
                "X-Audio-Encoding": "s16le",
            },
        )

    @app.post("/speak")
    async def speak(
        request: SpeakRequest, x_request_id: str | None = Header(default=None)
    ) -> Response:
        spec = resolve(request.voice_id)
        request_id = x_request_id or uuid.uuid4().hex[:12]
        payload = bytearray()
        chunks = await synthesize(spec, request.text.strip(), request_id)
        async for chunk in chunks:
            payload.extend(chunk)
        return Response(
            _wav(bytes(payload), spec.sample_rate),
            media_type="audio/wav",
            headers={"X-Request-ID": request_id},
        )

    @app.post("/v1/audio/speech")
    async def openai_speech(
        request: OpenAISpeechRequest, x_request_id: str | None = Header(default=None)
    ) -> Response:
        if request.response_format != "wav":
            raise HTTPException(
                status_code=400,
                detail=(
                    f"response_format {request.response_format!r} is unsupported; "
                    "Magpie returns 'wav' on the completed OpenAI-compatible route"
                ),
            )
        if request.speed != 1.0:
            raise HTTPException(
                status_code=400,
                detail=f"speed {request.speed} is unsupported; only 1.0 is accepted",
            )
        return await speak(
            SpeakRequest(voice_id=request.voice, text=request.input),
            x_request_id=x_request_id,
        )

    return app
