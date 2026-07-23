"""HTTP surface for the streaming voicebook service.

    POST /speak/stream  -> raw s16le PCM stream (LiveKit)
    POST /speak         -> completed audio/wav (Hermes)
    GET  /healthz       -> 503 while warming, 200 only after CUDA-graph warmup

Design invariants, each earned by a review cycle:

  * One-flight lease acquired SYNCHRONOUSLY before the response is built.
  * The reservation is owned by the ROUTE until the response is constructed and
    returned; any pre-handoff failure (including the constructor) closes the
    generator then the reservation.
  * The FIRST PCM chunk is prefetched while the route still owns the
    reservation. An empty stream is a typed 502, never a 200 with silence —
    that is the streaming form of the silent-truncation class we design
    against. The prefetched chunk is prepended, order preserved, no duplication.
  * Release lives in the response __call__ finally with NESTED teardown
    (close the generator, then always release the lease) so a raising close()
    cannot leak the lease. Correlation logging is inside the innermost finally,
    so even a teardown failure is logged.
  * Wire format is little-endian s16le, labelled application/octet-stream with
    explicit X-Audio-Format headers — NOT audio/L16 (big-endian, RFC 2586).
"""

from __future__ import annotations

import logging
import time
import uuid
from typing import TYPE_CHECKING

from fastapi import FastAPI, Header, HTTPException
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field
from starlette.responses import Response, StreamingResponse

from .lease import Busy, OneFlightLease, Reservation
from .registry import MasterIntegrityError, UnknownVoice, VoiceRegistry
from .synth import SAMPLE_RATE, SynthesisError, Synthesizer

if TYPE_CHECKING:
    from collections.abc import Iterator

    from starlette.types import Message, Receive, Scope, Send

MAX_INPUT_CHARS = 4000
MAX_REQUEST_ID = 64
logger = logging.getLogger("voicebook.stream")


class SpeakRequest(BaseModel):
    voice_id: str = Field(min_length=1)
    text: str = Field(min_length=1)


def _safe_rid(raw: str | None) -> str:
    if not raw:
        return uuid.uuid4().hex[:12]
    cleaned = "".join(c for c in raw if c.isalnum() or c in "-_")[:MAX_REQUEST_ID]
    return cleaned or uuid.uuid4().hex[:12]


class _PrependIter:
    """Yields a prefetched first chunk, then the rest, and closes the REAL
    backend generator on close() EVEN IF __next__ was never called.

    A generator-based prepend would reintroduce the unstarted-generator trap
    one layer down: after prefetch the backend is already started, but a
    generator wrapper that is never iterated skips its finally on close(),
    leaking the started GPU generator. A concrete class closes deterministically.
    """

    def __init__(self, first: bytes, rest: Iterator[bytes]) -> None:
        self._first = first
        self._rest = rest
        self._sent_first = False
        self._closed = False

    def __iter__(self) -> _PrependIter:
        return self

    def __next__(self) -> bytes:
        if not self._sent_first:
            self._sent_first = True
            return self._first
        return next(self._rest)

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        close = getattr(self._rest, "close", None)
        if callable(close):
            close()  # closes the started backend generator, iterated or not


class ReservationStreamingResponse(StreamingResponse):
    """Owns a Reservation + the body iterator; releases and logs on EVERY exit."""

    def __init__(
        self,
        reservation: Reservation,
        body: Iterator[bytes],
        *,
        request_id: str,
        voice_id: str,
        chars: int,
        start: float,
    ) -> None:
        self._reservation = reservation
        self._body = body
        self._rid = request_id
        self._voice_id = voice_id
        self._chars = chars
        self._start = start
        super().__init__(
            content=body,
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
        # Production (uvicorn HTTP) advertises ASGI spec_version 2.3, whose
        # StreamingResponse handles disconnect via a task group: it observes
        # http.disconnect on receive() and returns __call__ NORMALLY. So a
        # disconnect raises nothing here — we must detect it by watching
        # receive(), or the outcome would wrongly log "ok". (Spec >=2.4 instead
        # raises ClientDisconnect from send; both branches are covered by tests.)
        disconnected = {"seen": False}
        completed = {"body": False}

        async def watching_receive() -> Message:
            msg = await receive()
            if msg.get("type") == "http.disconnect":
                disconnected["seen"] = True
            return msg

        async def watching_send(message: Message) -> None:
            # Mark the body complete ONLY AFTER the terminal http.response.body
            # (more_body False) send actually returns. If that final send raises
            # or is cancelled, delivery was not proven complete — completed stays
            # False and the outcome remains disconnect/error, never a false
            # success. (F1, 2026-07-23.)
            is_final = message.get("type") == "http.response.body" and not message.get(
                "more_body", False
            )
            await send(message)
            if is_final:
                completed["body"] = True

        outcome = "ok"
        try:
            await super().__call__(scope, watching_receive, watching_send)
        except BaseException as exc:  # noqa: BLE001 - record then re-raise
            outcome = f"post-header:{type(exc).__name__}"
            raise
        finally:
            close_exc: BaseException | None = None
            try:
                close = getattr(self._body, "close", None)
                if callable(close):
                    close()
            except BaseException as exc:  # noqa: BLE001 - capture, log, release
                close_exc = exc
            # Final outcome precedence: a teardown failure is the most important
            # thing to correlate; then a normal-return disconnect (2.3 branch);
            # otherwise whatever the body path recorded.
            if close_exc is not None:
                outcome = f"teardown:{type(close_exc).__name__}"
            elif outcome == "ok" and disconnected["seen"] and not completed["body"]:
                # A disconnect is a real ABORT only if the body never completed.
                # A client closing AFTER full delivery also yields http.disconnect
                # but is a success, so completed["body"] takes precedence. (F1.)
                outcome = "disconnect"
            self._reservation.close()  # idempotent — always releases
            logger.info(
                "stream request_id=%s voice_id=%s chars=%d outcome=%s duration_ms=%d",
                self._rid,
                self._voice_id,
                self._chars,
                outcome,
                int((time.monotonic() - self._start) * 1000),
            )
            if close_exc is not None:
                raise close_exc


def create_app(registry: VoiceRegistry, synthesizer: Synthesizer, lease: OneFlightLease) -> FastAPI:
    app = FastAPI(title="voicebook-stream", version="0.1.0")
    reg = registry

    @app.get("/healthz")
    def healthz() -> JSONResponse:
        ready = synthesizer.ready
        return JSONResponse(
            status_code=200 if ready else 503,
            content={
                "status": "ok" if ready else "warming",
                "ready": ready,
                "voices": reg.voice_ids,
                "max_input_chars": MAX_INPUT_CHARS,
                "sample_rate": SAMPLE_RATE,
            },
        )

    def _log_pre(
        kind: str, rid: str, voice_id: str, chars: int, outcome: str, start: float
    ) -> None:
        logger.info(
            "%s request_id=%s voice_id=%s chars=%d outcome=%s duration_ms=%d",
            kind,
            rid,
            voice_id,
            chars,
            outcome,
            int((time.monotonic() - start) * 1000),
        )

    def _pre_header_checks(req: SpeakRequest, rid: str, kind: str, start: float) -> None:
        if len(req.text) > MAX_INPUT_CHARS:
            _log_pre(kind, rid, req.voice_id, len(req.text), "413_over_limit", start)
            raise HTTPException(
                status_code=413,
                detail=f"text is {len(req.text)} chars; limit {MAX_INPUT_CHARS}. Refused, never truncated.",
            )
        if not synthesizer.ready:
            _log_pre(kind, rid, req.voice_id, len(req.text), "503_warming", start)
            raise HTTPException(status_code=503, detail="warming up; not ready")

    def _resolve(voice_id: str):
        """Resolve + re-verify master hash. Raises typed HTTPException; does NOT
        log — the caller owns exactly one terminal correlation record."""
        try:
            return reg.get(voice_id)
        except UnknownVoice:
            raise HTTPException(
                status_code=404, detail=f"unknown voice_id {voice_id!r}; allowed: {reg.voice_ids}"
            ) from None
        except MasterIntegrityError as exc:
            raise HTTPException(status_code=500, detail=str(exc)) from None

    @app.post("/speak/stream")
    def speak_stream(
        req: SpeakRequest, x_request_id: str | None = Header(default=None)
    ) -> Response:
        rid = _safe_rid(x_request_id)
        start = time.monotonic()
        chars = len(req.text)
        _pre_header_checks(req, rid, "stream", start)
        try:
            reservation = lease.reserve()
        except Busy:
            _log_pre("stream", rid, req.voice_id, chars, "429_busy", start)
            raise HTTPException(
                status_code=429, detail="a generation is already in flight"
            ) from None
        # Reservation held by the ROUTE through prefetch and response construction.
        sync_gen = None
        try:
            entry = _resolve(req.voice_id)
            sync_gen = synthesizer.synthesize_stream(
                req.text, entry.master_path, entry.reference_transcript
            )
            try:
                first = next(iter(sync_gen))
            except StopIteration:
                _log_pre("stream", rid, req.voice_id, chars, "502_empty", start)
                raise HTTPException(status_code=502, detail="backend produced no audio") from None
            except SynthesisError as exc:
                _log_pre("stream", rid, req.voice_id, chars, "502_synth", start)
                raise HTTPException(status_code=502, detail=f"synthesis failed: {exc}") from None
            except Exception as exc:  # noqa: BLE001 - normalize backend errors
                _log_pre("stream", rid, req.voice_id, chars, "502_other", start)
                raise HTTPException(
                    status_code=502, detail=f"synthesis failed: {type(exc).__name__}: {exc}"
                ) from None
            body = _PrependIter(first, sync_gen)
            resp = ReservationStreamingResponse(
                reservation,
                body,
                request_id=rid,
                voice_id=req.voice_id,
                chars=chars,
                start=start,
            )
        except HTTPException as exc:
            # typed pre-handoff failure (404/500, or 502 from prefetch already
            # logged) — close the started backend if any, release, log once.
            if exc.status_code in (404, 500):
                _log_pre("stream", rid, req.voice_id, chars, f"{exc.status_code}", start)
            try:
                if sync_gen is not None and callable(getattr(sync_gen, "close", None)):
                    sync_gen.close()
            finally:
                reservation.close()
            raise
        except BaseException:
            try:
                if sync_gen is not None and callable(getattr(sync_gen, "close", None)):
                    sync_gen.close()
            finally:
                reservation.close()
            raise
        return resp

    @app.post("/speak")
    def speak(req: SpeakRequest, x_request_id: str | None = Header(default=None)) -> Response:
        rid = _safe_rid(x_request_id)
        start = time.monotonic()
        _pre_header_checks(req, rid, "unary", start)
        try:
            reservation = lease.reserve()
        except Busy:
            _log_pre("unary", rid, req.voice_id, len(req.text), "429_busy", start)
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
            outcome = str(exc.status_code)
            raise
        except SynthesisError as exc:
            outcome = "502_synth"
            raise HTTPException(status_code=502, detail=f"synthesis failed: {exc}") from None
        except Exception as exc:  # noqa: BLE001 - normalize; BaseException preserved
            outcome = "502_other"
            raise HTTPException(
                status_code=502, detail=f"synthesis failed: {type(exc).__name__}: {exc}"
            ) from None
        finally:
            reservation.close()
            _log_pre("unary", rid, req.voice_id, len(req.text), outcome, start)

    return app
