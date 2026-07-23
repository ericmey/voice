"""HTTP surface for the streaming voicebook service.

Two routes, one identity, one lease:
    POST /speak/stream  {"voice_id","text"} -> 24kHz mono PCM16 stream (LiveKit)
    POST /speak         {"voice_id","text"} -> completed audio/wav (Hermes)
    GET  /healthz       -> green ONLY after CUDA-graph warmup

Lease ownership is the whole game here (three review cycles found three
lifecycle traps in it):

  * Acquisition is SYNCHRONOUS in the route, before the response is built, so a
    collision is a clean typed 429 — never a half-sent 200.
  * The reservation is owned by the ROUTE until the response is successfully
    constructed and returned, then handed to the RESPONSE. If anything throws
    between acquire and that handoff, the route closes it immediately.
  * For the stream, release lives in a response __call__ finally — NOT a body-
    generator finally, which does not run when the generator is never started.
    That finally also closes the underlying faster-qwen generator, so a
    disconnect deterministically tears down the GPU pull.

Pre-header failures (Busy, not-ready, unknown-voice, over-limit, bad-hash) are
typed HTTP errors because headers are not yet sent. Post-header failures
(backend error mid-stream, disconnect) cannot change the status; the stream
terminates and the reservation closes via the response finally.
"""

from __future__ import annotations

import uuid
from typing import TYPE_CHECKING

from fastapi import FastAPI, Header, HTTPException, Response
from pydantic import BaseModel, Field
from starlette.responses import StreamingResponse

from .lease import Busy, OneFlightLease, Reservation
from .registry import MasterIntegrityError, UnknownVoice, VoiceRegistry
from .synth import SAMPLE_RATE, SynthesisError, Synthesizer

if TYPE_CHECKING:
    from collections.abc import Iterator

    from starlette.types import Receive, Scope, Send

MAX_INPUT_CHARS = 4000


class SpeakRequest(BaseModel):
    voice_id: str = Field(min_length=1)
    text: str = Field(min_length=1)


class ReservationStreamingResponse(StreamingResponse):
    """StreamingResponse that owns a Reservation and a sync PCM generator.

    __call__'s finally releases the lease and closes the generator on EVERY
    exit: response-start failure, client disconnect (OSError from send),
    backend error during iteration, and normal completion — and even if the
    body iterator was never started."""

    def __init__(
        self,
        reservation: Reservation,
        sync_gen: Iterator[bytes],
        *,
        request_id: str,
    ) -> None:
        self._reservation = reservation
        self._sync_gen = sync_gen
        super().__init__(
            content=sync_gen,
            media_type=f"audio/L16; rate={SAMPLE_RATE}; channels=1",
            headers={"X-Request-ID": request_id, "Cache-Control": "no-store"},
        )

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        try:
            await super().__call__(scope, receive, send)
        finally:
            close = getattr(self._sync_gen, "close", None)
            if callable(close):
                close()  # deterministic GPU teardown, even mid-stream
            self._reservation.close()  # idempotent — safe on any exit path


def create_app(registry: VoiceRegistry, synthesizer: Synthesizer, lease: OneFlightLease) -> FastAPI:
    app = FastAPI(title="voicebook-stream", version="0.1.0")
    reg = registry
    is_ready = getattr(synthesizer, "ready", True)

    @app.get("/healthz")
    def healthz() -> dict:
        # Green ONLY after warmup. ready is a live property on the real backend.
        ready = getattr(synthesizer, "ready", True)
        return {
            "status": "ok" if ready else "warming",
            "ready": ready,
            "voices": reg.voice_ids,
            "max_input_chars": MAX_INPUT_CHARS,
            "sample_rate": SAMPLE_RATE,
        }

    def _pre_header_checks(req: SpeakRequest) -> None:
        """Everything that can be a typed error BEFORE any response is built."""
        if len(req.text) > MAX_INPUT_CHARS:
            raise HTTPException(
                status_code=413,
                detail=(
                    f"text is {len(req.text)} chars; limit {MAX_INPUT_CHARS}. "
                    "Refused, never truncated."
                ),
            )
        if not getattr(synthesizer, "ready", True):
            raise HTTPException(status_code=503, detail="warming up; not ready")

    def _resolve(voice_id: str):
        try:
            return reg.get(voice_id)  # re-verifies master hash before use
        except UnknownVoice:
            raise HTTPException(
                status_code=404,
                detail=f"unknown voice_id {voice_id!r}; allowed: {reg.voice_ids}",
            ) from None
        except MasterIntegrityError as exc:
            raise HTTPException(status_code=500, detail=str(exc)) from None

    @app.post("/speak/stream")
    def speak_stream(
        req: SpeakRequest, x_request_id: str | None = Header(default=None)
    ) -> Response:
        rid = x_request_id or uuid.uuid4().hex[:12]
        _pre_header_checks(req)
        try:
            reservation = lease.reserve()  # Busy -> 429, no reservation held
        except Busy:
            raise HTTPException(
                status_code=429, detail="a generation is already in flight"
            ) from None
        # Reservation now held by the ROUTE. Any failure before handoff closes it.
        try:
            entry = _resolve(req.voice_id)
            sync_gen = synthesizer.synthesize_stream(
                req.text, entry.master_path, entry.reference_transcript
            )
        except BaseException:
            reservation.close()
            raise
        # Ownership handed to the response; its __call__ finally releases.
        return ReservationStreamingResponse(reservation, sync_gen, request_id=rid)

    @app.post("/speak")
    def speak(req: SpeakRequest, x_request_id: str | None = Header(default=None)) -> Response:
        rid = x_request_id or uuid.uuid4().hex[:12]
        _pre_header_checks(req)
        try:
            reservation = lease.reserve()
        except Busy:
            raise HTTPException(
                status_code=429, detail="a generation is already in flight"
            ) from None
        try:
            entry = _resolve(req.voice_id)
            wav = synthesizer.synthesize(req.text, entry.master_path, entry.reference_transcript)
            if not wav:
                raise HTTPException(status_code=502, detail="backend produced no audio")
            return Response(
                content=wav,
                media_type="audio/wav",
                headers={"X-Request-ID": rid},
            )
        except SynthesisError as exc:
            raise HTTPException(status_code=502, detail=f"synthesis failed: {exc}") from None
        finally:
            reservation.close()  # unary path: always releases before returning

    _ = is_ready  # documents that readiness is captured per-request, not cached
    return app
