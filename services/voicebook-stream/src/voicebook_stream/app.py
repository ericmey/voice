"""HTTP surface for the streaming voicebook service.

    POST /speak/stream  {"voice_id","text"} -> raw s16le PCM stream (LiveKit)
    POST /speak         {"voice_id","text"} -> completed audio/wav (Hermes)
    GET  /healthz       -> 503 while warming, 200 only after CUDA-graph warmup

Lease ownership is the whole game (multiple review cycles found lifecycle traps):
  * Acquisition is SYNCHRONOUS in the route, before the response is built.
  * The reservation is owned by the ROUTE until the response is constructed AND
    returned; any failure before that handoff — including the response
    constructor — closes the generator then the reservation.
  * Release lives in a response __call__ finally (not a body-generator finally,
    which does not run for an unstarted generator). Teardown is NESTED:
    try close(generator) / finally reservation.close(), so a raising close()
    cannot leak the lease.

Wire format: the stream is little-endian s16le PCM. It is NOT labelled audio/L16
— RFC 2586 L16 is big-endian network order, and mislabelling it would be a
false wire contract. It is application/octet-stream with explicit
X-Audio-Format / X-Sample-Rate / X-Channels headers.
"""

from __future__ import annotations

import logging
import time
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
MAX_REQUEST_ID = 64
logger = logging.getLogger("voicebook.stream")


class SpeakRequest(BaseModel):
    voice_id: str = Field(min_length=1)
    text: str = Field(min_length=1)


def _safe_rid(raw: str | None) -> str:
    """Cap and sanitize a client-supplied request id; generate if absent."""
    if not raw:
        return uuid.uuid4().hex[:12]
    cleaned = "".join(c for c in raw if c.isalnum() or c in "-_")[:MAX_REQUEST_ID]
    return cleaned or uuid.uuid4().hex[:12]


class ReservationStreamingResponse(StreamingResponse):
    """StreamingResponse that owns a Reservation and a sync PCM generator, and
    releases both on EVERY exit path with teardown that cannot itself leak."""

    def __init__(
        self, reservation: Reservation, sync_gen: Iterator[bytes], *, request_id: str
    ) -> None:
        self._reservation = reservation
        self._sync_gen = sync_gen
        self._rid = request_id
        super().__init__(
            content=sync_gen,
            media_type="application/octet-stream",
            headers={
                "X-Request-ID": request_id,
                "X-Audio-Format": "s16le",
                "X-Sample-Rate": str(SAMPLE_RATE),
                "X-Channels": "1",
                "Cache-Control": "no-store",
            },
        )

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        outcome = "ok"
        try:
            await super().__call__(scope, receive, send)
        except BaseException as exc:  # noqa: BLE001 - log then re-raise
            outcome = f"post-header:{type(exc).__name__}"
            raise
        finally:
            # NESTED teardown: a raising generator close() must NOT prevent the
            # lease release. reproduced leak otherwise.
            try:
                close = getattr(self._sync_gen, "close", None)
                if callable(close):
                    close()
            finally:
                self._reservation.close()  # idempotent
            logger.info("stream request_id=%s outcome=%s", self._rid, outcome)


def create_app(registry: VoiceRegistry, synthesizer: Synthesizer, lease: OneFlightLease) -> FastAPI:
    app = FastAPI(title="voicebook-stream", version="0.1.0")
    reg = registry

    @app.get("/healthz")
    def healthz() -> Response:
        ready = synthesizer.ready  # fail-closed: Protocol requires it
        body = (
            f'{{"status":"{"ok" if ready else "warming"}","ready":{str(ready).lower()},'
            f'"voices":{reg.voice_ids!r},"max_input_chars":{MAX_INPUT_CHARS},'
            f'"sample_rate":{SAMPLE_RATE}}}'
        ).replace("'", '"')
        return Response(
            content=body,
            media_type="application/json",
            status_code=200 if ready else 503,
        )

    def _pre_header_checks(req: SpeakRequest) -> None:
        if len(req.text) > MAX_INPUT_CHARS:
            raise HTTPException(
                status_code=413,
                detail=f"text is {len(req.text)} chars; limit {MAX_INPUT_CHARS}. Refused, never truncated.",
            )
        if not synthesizer.ready:
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
        rid = _safe_rid(x_request_id)
        _pre_header_checks(req)
        try:
            reservation = lease.reserve()
        except Busy:
            logger.info("stream request_id=%s outcome=429_busy", rid)
            raise HTTPException(
                status_code=429, detail="a generation is already in flight"
            ) from None
        # Reservation held by the ROUTE. Any failure before the response is
        # RETURNED — including the response constructor — closes gen then lease.
        try:
            entry = _resolve(req.voice_id)
            sync_gen = synthesizer.synthesize_stream(
                req.text, entry.master_path, entry.reference_transcript
            )
            resp = ReservationStreamingResponse(reservation, sync_gen, request_id=rid)
        except BaseException:
            try:
                g = locals().get("sync_gen")
                if g is not None and callable(getattr(g, "close", None)):
                    g.close()
            finally:
                reservation.close()
            raise
        return resp

    @app.post("/speak")
    def speak(req: SpeakRequest, x_request_id: str | None = Header(default=None)) -> Response:
        rid = _safe_rid(x_request_id)
        t0 = time.monotonic()
        _pre_header_checks(req)
        try:
            reservation = lease.reserve()
        except Busy:
            logger.info("unary request_id=%s outcome=429_busy", rid)
            raise HTTPException(
                status_code=429, detail="a generation is already in flight"
            ) from None
        outcome = "ok"
        try:
            entry = _resolve(req.voice_id)
            wav = synthesizer.synthesize(req.text, entry.master_path, entry.reference_transcript)
            if not wav:
                outcome = "502_empty"
                raise HTTPException(status_code=502, detail="backend produced no audio")
            return Response(content=wav, media_type="audio/wav", headers={"X-Request-ID": rid})
        except HTTPException as exc:
            outcome = f"{exc.status_code}"
            raise
        except SynthesisError as exc:
            outcome = "502_synth"
            raise HTTPException(status_code=502, detail=f"synthesis failed: {exc}") from None
        except Exception as exc:  # noqa: BLE001 - normalize, preserve BaseException
            outcome = "502_other"
            raise HTTPException(
                status_code=502, detail=f"synthesis failed: {type(exc).__name__}: {exc}"
            ) from None
        finally:
            reservation.close()
            logger.info(
                "unary request_id=%s voice_id=%s chars=%d outcome=%s duration_ms=%d",
                rid,
                req.voice_id,
                len(req.text),
                outcome,
                int((time.monotonic() - t0) * 1000),
            )

    return app
