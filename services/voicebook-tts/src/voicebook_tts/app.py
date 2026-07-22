"""HTTP surface for the voicebook TTS service.

Contract, deliberately narrow:

    POST /speak   {"voice_id": "...", "text": "..."}  -> audio/wav
    GET  /voices                                      -> allowlisted ids
    GET  /healthz                                     -> liveness + registry state

Design rules, each of which exists because of a defect we actually hit:

* Client sends ``voice_id`` only. Never a path, never a hash.
* Over-limit input FAILS with a typed 413. It is never truncated — a silently
  shortened daily summary is the worst outcome available, because it reads as
  success.
* One generation in flight. Concurrency is unproven, so it is refused (429)
  rather than queued invisibly.
* No output is written or returned on failure.
"""

from __future__ import annotations

import threading

from fastapi import FastAPI, HTTPException, Response
from pydantic import BaseModel, Field

from .registry import MasterIntegrityError, UnknownVoice, VoiceRegistry
from .synth import SynthesisError, Synthesizer

#: Hard ceiling on a single request.
#:
#: PROVISIONAL AND UNMEASURED. An earlier version of this comment claimed the
#: value had been "measured against a real long-form Nyla daily summary." It had
#: not. Nothing was measured; 4000 was picked. A comment asserting a check that
#: never ran is worse than no comment, because it stops the next reader looking.
#:
#: To settle it: render a real Nyla daily summary, count its characters, and
#: either justify this number or move it. Until then it is a guess with a guard
#: around it — the guard (typed 413, never truncate) is the part that is real.
MAX_INPUT_CHARS = 4000


class SpeakRequest(BaseModel):
    voice_id: str = Field(min_length=1)
    text: str = Field(min_length=1)


def create_app(registry: VoiceRegistry, synthesizer: Synthesizer) -> FastAPI:
    app = FastAPI(title="voicebook-tts", version="0.1.0")

    # One generation at a time. Non-blocking acquire so a second caller is told
    # "busy" immediately instead of silently waiting behind an unbounded queue.
    gen_lock = threading.Lock()

    # Dependencies are injected into create_app and closed over. Endpoint-level
    # Depends() was tried first and silently degraded `reg` into a QUERY
    # parameter under `from __future__ import annotations`, turning every
    # request into a 422. A closure has no such failure mode.
    reg = registry

    @app.get("/healthz")
    def healthz() -> dict:
        return {"status": "ok", "voices": reg.voice_ids, "max_input_chars": MAX_INPUT_CHARS}

    @app.get("/voices")
    def voices() -> dict:
        return {"voices": reg.voice_ids}

    @app.post("/speak")
    def speak(req: SpeakRequest) -> Response:
        if len(req.text) > MAX_INPUT_CHARS:
            raise HTTPException(
                status_code=413,
                detail=(
                    f"text is {len(req.text)} chars; limit is {MAX_INPUT_CHARS}. "
                    "Refused rather than truncated — a silently shortened message "
                    "reads as success."
                ),
            )

        try:
            entry = reg.get(req.voice_id)
        except UnknownVoice:
            raise HTTPException(
                status_code=404,
                detail=f"unknown voice_id {req.voice_id!r}; allowed: {reg.voice_ids}",
            ) from None
        except MasterIntegrityError as exc:
            # 500, not 4xx: the caller did nothing wrong, our deployed master is
            # not the accepted voice.
            raise HTTPException(status_code=500, detail=str(exc)) from None

        if not gen_lock.acquire(blocking=False):
            raise HTTPException(
                status_code=429,
                detail="a generation is already in flight; concurrency is unproven",
            )
        try:
            audio = synthesizer.speak(req.text, entry.master_path, entry.reference_transcript)
        except SynthesisError as exc:
            raise HTTPException(status_code=502, detail=f"synthesis failed: {exc}") from None
        except HTTPException:
            raise
        except Exception as exc:  # noqa: BLE001 - deliberate
            # A Protocol cannot enforce which exception type an implementation
            # raises. Catching only SynthesisError meant a backend raising
            # anything else (CUDA OOM, tokenizer error) escaped as an untyped
            # 500 while this module advertised a typed 502. Caught by test, not
            # by reading — the fake synthesizer raised the correct type and so
            # could never have exposed it.
            raise HTTPException(
                status_code=502, detail=f"synthesis failed: {type(exc).__name__}: {exc}"
            ) from None
        finally:
            gen_lock.release()

        if not audio:
            raise HTTPException(status_code=502, detail="backend produced empty audio")

        return Response(content=audio, media_type="audio/wav")

    return app
