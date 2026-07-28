"""OpenAI-shaped transcription in front of Riva/Parakeet streaming gRPC.

    POST /v1/audio/transcriptions  -> {"text": "..."}  (OpenAI multipart in)
    GET  /healthz                  -> 503 until the gRPC backend answers

Why this exists
---------------
Our deployed Parakeet NIM runs the `mode=str` profile. It publishes its own
/v1/audio/transcriptions, but that route needs an OFFLINE model registered and
ours has none, so it answers "Model not found for language en". Streaming gRPC
is the contract the profile actually serves. Every harness we run (Hermes,
OpenClaw) speaks OpenAI-shaped HTTP. This service is the seam between the two --
nothing more. It holds no model and no state.

Contracts it keeps, deliberately
--------------------------------
  * An unusable upload is a typed 4xx. It is never transcribed as best-effort:
    a wrong-sample-rate stream returns fluent, confident, wrong text, which is
    strictly worse than an error.
  * A backend failure is a 502 carrying the real reason. It is never an empty
    transcript, because "" reads to a caller as "silence" -- a success.
  * Readiness is measured against the backend, not against this process being
    up. A shim that reports healthy while its backend is down is the exact
    failure shape this fleet keeps finding.
"""

from __future__ import annotations

import logging
import os
import time
import uuid
from collections.abc import Callable
from typing import Protocol

from fastapi import FastAPI, File, Form, HTTPException, UploadFile
from fastapi.responses import JSONResponse, PlainTextResponse
from starlette.responses import Response

from .asr import AsrError, RivaTranscriber, Transcript
from .audio import AudioError, to_riva_pcm

logger = logging.getLogger("parakeet.openai")

MAX_UPLOAD_BYTES = int(os.environ.get("PARAKEET_MAX_UPLOAD_BYTES", 25 * 1024 * 1024))
SUPPORTED_FORMATS = ("json", "text", "verbose_json")


class Transcriber(Protocol):
    uri: str
    language: str

    def check_ready(self) -> None: ...

    def transcribe(self, pcm: bytes) -> Transcript: ...


def create_app(
    transcriber_factory: Callable[[], Transcriber] = RivaTranscriber,
) -> FastAPI:
    """Build the app. The factory is injectable so tests never need the SDK."""
    app = FastAPI(title="parakeet-openai", version="0.1.0")
    state: dict = {"transcriber": None, "error": None}

    def _backend():
        """Construct the Riva client lazily and remember why it failed.

        Constructing at import time would make the container crash-loop when
        Parakeet is merely slow to come up, and would hide the reason behind a
        restart counter instead of putting it in /healthz.
        """
        if state["transcriber"] is None:
            try:
                state["transcriber"] = transcriber_factory()
                state["error"] = None
            except Exception as exc:  # noqa: BLE001 - surfaced, not swallowed
                state["error"] = f"{type(exc).__name__}: {exc}"
                raise HTTPException(
                    status_code=503, detail=f"ASR backend unavailable: {state['error']}"
                ) from None
        backend = state["transcriber"]
        try:
            backend.check_ready()
            state["error"] = None
        except Exception as exc:  # noqa: BLE001 - surfaced, not swallowed
            state["error"] = f"{type(exc).__name__}: {exc}"
            raise HTTPException(
                status_code=503, detail=f"ASR backend unavailable: {state['error']}"
            ) from None
        return backend

    @app.get("/healthz")
    def healthz() -> JSONResponse:
        try:
            backend = _backend()
        except HTTPException:
            return JSONResponse(
                status_code=503,
                content={"status": "unavailable", "ready": False, "error": state["error"]},
            )
        return JSONResponse(
            status_code=200,
            content={
                "status": "ok",
                "ready": True,
                "backend": backend.uri,
                "language": backend.language,
                "max_upload_bytes": MAX_UPLOAD_BYTES,
            },
        )

    @app.post("/v1/audio/transcriptions")
    async def transcriptions(
        file: UploadFile = File(...),
        model: str = Form("parakeet"),
        language: str | None = Form(None),
        prompt: str | None = Form(None),
        response_format: str = Form("json"),
        temperature: float = Form(0.0),
    ) -> Response:
        rid = uuid.uuid4().hex[:12]
        start = time.monotonic()

        def log(outcome: str, chars: int = 0) -> None:
            logger.info(
                "transcribe request_id=%s model=%s filename=%s outcome=%s chars=%d duration_ms=%d",
                rid,
                model,
                file.filename,
                outcome,
                chars,
                int((time.monotonic() - start) * 1000),
            )

        if response_format not in SUPPORTED_FORMATS:
            log("400_format")
            raise HTTPException(
                status_code=400,
                detail=f"response_format {response_format!r} unsupported; "
                f"choose one of {list(SUPPORTED_FORMATS)}",
            )

        data = await file.read()
        if len(data) > MAX_UPLOAD_BYTES:
            log("413_too_large")
            raise HTTPException(
                status_code=413,
                detail=f"upload is {len(data)} bytes; limit {MAX_UPLOAD_BYTES}. "
                "Refused, never truncated.",
            )

        try:
            pcm, seconds = to_riva_pcm(data)
        except AudioError as exc:
            log("400_audio")
            raise HTTPException(status_code=400, detail=str(exc)) from None

        backend = _backend()
        try:
            result = backend.transcribe(pcm)
        except AsrError as exc:
            log("502_asr")
            raise HTTPException(status_code=502, detail=f"transcription failed: {exc}") from None

        # An empty transcript is REPORTED as empty, not dressed up. Silence is a
        # legitimate answer; the caller gets to tell the difference between
        # "nothing was said" and "the backend fell over" because the latter is a
        # 502 above and never reaches here.
        log("ok", len(result.text))

        if response_format == "text":
            return PlainTextResponse(result.text, headers={"X-Request-ID": rid})
        if response_format == "verbose_json":
            return JSONResponse(
                content={
                    "task": "transcribe",
                    "language": language or backend.language,
                    "duration": round(seconds, 3),
                    "text": result.text,
                },
                headers={"X-Request-ID": rid},
            )
        return JSONResponse(content={"text": result.text}, headers={"X-Request-ID": rid})

    return app


app = create_app()
