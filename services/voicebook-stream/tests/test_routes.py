"""Route + response-ownership lifecycle. No GPU.

The four proofs Yua required, at the layer she scoped:
  collision -> 429; failure before first chunk releases; disconnect after a
  chunk releases; backend error releases. Plus typed pre-header failures.

Disconnect and response-start failure are exercised by driving the response's
ASGI __call__ with a controllable fake send() — deterministic, no real socket.
"""

from __future__ import annotations

import asyncio
import hashlib
import wave
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from starlette.requests import ClientDisconnect
from voicebook_stream.app import ReservationStreamingResponse, create_app
from voicebook_stream.lease import OneFlightLease
from voicebook_stream.registry import VoiceEntry, VoiceRegistry
from voicebook_stream.synth import SynthesisError


def _wav(p: Path) -> str:
    with wave.open(str(p), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(24000)
        w.writeframes(b"\x01\x00" * 2400)
    return hashlib.sha256(p.read_bytes()).hexdigest()


class FakeSynth:
    ready = True

    def __init__(self, *, fail=False, n=3):
        self.fail, self.n = fail, n
        self.closed = False

    def _gen(self):
        try:
            if self.fail:
                raise SynthesisError("backend exploded")
            for i in range(self.n):
                yield f"pcm{i}".encode()
        finally:
            self.closed = True

    def synthesize_stream(self, text, master_path, reference_transcript):
        self._live = self._gen()
        return self._live

    def synthesize(self, text, master_path, reference_transcript):
        if self.fail:
            raise SynthesisError("backend exploded")
        return b"RIFF" + b"\x00" * 64


@pytest.fixture
def env(tmp_path):
    m = tmp_path / "sumi.wav"
    digest = _wav(m)
    reg = VoiceRegistry({"sumi-v1": VoiceEntry("sumi-v1", m, "ref", digest)})
    lease = OneFlightLease()
    synth = FakeSynth()
    app = create_app(reg, synth, lease)
    return reg, synth, lease, TestClient(app)


# --- pre-header typed failures ------------------------------------------------


def test_unknown_voice_is_404_and_releases(env):
    reg, synth, lease, client = env
    r = client.post("/speak/stream", json={"voice_id": "nobody", "text": "hi"})
    assert r.status_code == 404
    assert lease.locked is False  # reservation released on pre-handoff failure


def test_over_limit_is_413_no_reservation(env):
    reg, synth, lease, client = env
    r = client.post("/speak/stream", json={"voice_id": "sumi-v1", "text": "x" * 4001})
    assert r.status_code == 413
    assert lease.locked is False


def test_not_ready_is_503(tmp_path):
    m = tmp_path / "s.wav"
    d = _wav(m)
    reg = VoiceRegistry({"x": VoiceEntry("x", m, "t", d)})
    lease = OneFlightLease()
    synth = FakeSynth()
    synth.ready = False
    client = TestClient(create_app(reg, synth, lease))
    r = client.post("/speak/stream", json={"voice_id": "x", "text": "hi"})
    assert r.status_code == 503
    assert lease.locked is False


def test_healthz_red_until_ready(tmp_path):
    m = tmp_path / "s.wav"
    d = _wav(m)
    reg = VoiceRegistry({"x": VoiceEntry("x", m, "t", d)})
    synth = FakeSynth()
    synth.ready = False
    client = TestClient(create_app(reg, synth, OneFlightLease()))
    warming = client.get("/healthz")
    assert warming.status_code == 503  # curl -f / docker healthcheck fail
    assert warming.json()["ready"] is False
    synth.ready = True
    ok = client.get("/healthz")
    assert ok.status_code == 200
    assert ok.json()["ready"] is True


# --- streaming happy path -----------------------------------------------------


def test_stream_returns_pcm_and_releases_after(env):
    reg, synth, lease, client = env
    with client.stream("POST", "/speak/stream", json={"voice_id": "sumi-v1", "text": "hi"}) as r:
        body = b"".join(r.iter_bytes())
    assert r.status_code == 200
    assert body == b"pcm0pcm1pcm2"  # decoded bytes, not content-type
    assert r.headers["content-type"] == "application/octet-stream"
    assert r.headers["x-audio-format"] == "s16le"
    assert r.headers["x-sample-rate"] == "24000"
    assert lease.locked is False  # released after completion
    assert synth.closed is True  # generator closed


def test_completed_wav_route(env):
    reg, synth, lease, client = env
    r = client.post("/speak", json={"voice_id": "sumi-v1", "text": "hi"})
    assert r.status_code == 200
    assert r.headers["content-type"] == "audio/wav"
    assert lease.locked is False


# --- the four lifecycle proofs at the response-ownership layer -----------------


def _drive(resp, sends):
    """Run a response's ASGI __call__ with a scripted fake send()."""
    scope = {"type": "http", "asgi": {"spec_version": "2.4"}}

    async def receive():
        return {"type": "http.request"}

    async def send(msg):
        action = sends.pop(0) if sends else "ok"
        if action == "raise":
            raise OSError("client disconnect")

    return asyncio.get_event_loop().run_until_complete(resp(scope, receive, send))


def _mkresp(res, gen, rid="rid"):
    """Construct the response with the current required metadata kwargs."""
    import time as _t

    return ReservationStreamingResponse(
        res, gen, request_id=rid, voice_id="sumi-v1", chars=2, start=_t.monotonic()
    )


def test_lifecycle_normal_completion_releases(env):
    reg, synth, lease, _ = env
    r = reg.get("sumi-v1")
    res = lease.reserve()
    gen = synth.synthesize_stream("hi", r.master_path, r.reference_transcript)
    resp = _mkresp(res, gen)
    _drive(resp, [])  # all sends succeed
    assert lease.locked is False and synth.closed is True


def test_lifecycle_disconnect_before_first_chunk_releases(env):
    reg, synth, lease, _ = env
    r = reg.get("sumi-v1")
    res = lease.reserve()
    gen = synth.synthesize_stream("hi", r.master_path, r.reference_transcript)
    resp = _mkresp(res, gen)
    # send #1 is response.start -> raise: disconnect before any body chunk
    with pytest.raises(ClientDisconnect):
        _drive(resp, ["raise"])
    assert lease.locked is False, "lease leaked when disconnect hit before first chunk"


def test_lifecycle_disconnect_after_chunk_releases(env):
    reg, synth, lease, _ = env
    r = reg.get("sumi-v1")
    res = lease.reserve()
    gen = synth.synthesize_stream("hi", r.master_path, r.reference_transcript)
    resp = _mkresp(res, gen)
    # start ok, first body ok, second body -> raise (disconnect mid-stream)
    with pytest.raises(ClientDisconnect):
        _drive(resp, ["ok", "ok", "raise"])
    assert lease.locked is False
    assert synth.closed is True, "GPU generator not torn down on mid-stream disconnect"


def test_lifecycle_backend_error_releases(env):
    reg, lease = env[0], env[2]
    synth = FakeSynth(fail=True)
    r = reg.get("sumi-v1")
    res = lease.reserve()
    gen = synth.synthesize_stream("hi", r.master_path, r.reference_transcript)
    resp = _mkresp(res, gen)
    with pytest.raises(SynthesisError):
        _drive(resp, [])  # sends fine, but the generator raises on iteration
    assert lease.locked is False


def test_active_collision_returns_429(env):
    """Second request while the lease is held mid-stream gets 429, not 200."""
    reg, synth, lease, client = env
    lease.reserve()  # simulate request 1 actively holding the lease
    r = client.post("/speak/stream", json={"voice_id": "sumi-v1", "text": "hi"})
    assert r.status_code == 429


# --- blocker fixes: close-raises, constructor failure, empty stream ------------


class _ExplodingCloseIter:
    """Iterator whose close() raises. A generator's close is read-only, so use
    a real object to prove the nested-teardown ordering."""

    def __init__(self):
        self._items = iter([b"one"])

    def __iter__(self):
        return self

    def __next__(self):
        return next(self._items)

    def close(self):
        raise RuntimeError("teardown blew up")


class ExplodingCloseSynth:
    ready = True

    def synthesize_stream(self, text, master_path, reference_transcript):
        return _ExplodingCloseIter()

    def synthesize(self, text, master_path, reference_transcript):
        return b"RIFF"


def test_lease_releases_even_if_generator_close_raises(env):
    """Blocker 1: a raising gen.close() must NOT leak the lease."""
    reg, _synth, lease, _ = env
    r = reg.get("sumi-v1")
    res = lease.reserve()
    gen = ExplodingCloseSynth().synthesize_stream("hi", r.master_path, r.reference_transcript)
    resp = _mkresp(res, gen)
    with pytest.raises(RuntimeError, match="teardown blew up"):
        _drive(resp, [])
    assert lease.locked is False, "lease leaked when close() raised"


class EmptyStreamSynth:
    ready = True
    closed = False

    def synthesize_stream(self, text, master_path, reference_transcript):
        def gen():
            self.closed = True
            return
            yield  # unreachable — empty stream

        return gen()

    def synthesize(self, text, master_path, reference_transcript):
        return b""


def test_empty_stream_is_502_not_silent_200(tmp_path):
    """Empty stream must be a typed 502, never a 200 with silence — the
    streaming form of the silent-truncation class. And it must release."""
    m = tmp_path / "s.wav"
    d = _wav(m)
    reg = VoiceRegistry({"x": VoiceEntry("x", m, "t", d)})
    lease = OneFlightLease()
    synth = EmptyStreamSynth()
    client = TestClient(create_app(reg, synth, lease), raise_server_exceptions=False)
    r = client.post("/speak/stream", json={"voice_id": "x", "text": "hi"})
    assert r.status_code == 502
    assert lease.locked is False
    assert synth.closed is True  # generator was closed on the empty path


def test_stream_first_chunk_prefetched_no_loss_no_dup(env):
    """The prefetched first chunk must be prepended exactly once, in order."""
    reg, synth, lease, client = env  # FakeSynth yields pcm0,pcm1,pcm2
    with client.stream("POST", "/speak/stream", json={"voice_id": "sumi-v1", "text": "hi"}) as r:
        body = b"".join(r.iter_bytes())
    assert body == b"pcm0pcm1pcm2"  # first chunk not lost, not duplicated
    assert lease.locked is False


def test_unary_arbitrary_exception_maps_to_502(tmp_path):
    """Blocker: a non-SynthesisError backend exception -> typed 502, not 500."""
    m = tmp_path / "s.wav"
    d = _wav(m)
    reg = VoiceRegistry({"x": VoiceEntry("x", m, "t", d)})

    class Boom:
        ready = True

        def synthesize(self, text, master_path, reference_transcript):
            raise RuntimeError("cuda oom")

        def synthesize_stream(self, text, master_path, reference_transcript):
            yield b""

    client = TestClient(create_app(reg, Boom(), OneFlightLease()), raise_server_exceptions=False)
    r = client.post("/speak", json={"voice_id": "x", "text": "hi"})
    assert r.status_code == 502


def test_request_id_is_capped_and_sanitized(env):
    from voicebook_stream.app import MAX_REQUEST_ID, _safe_rid

    assert len(_safe_rid("x" * 500)) <= MAX_REQUEST_ID
    assert _safe_rid("../etc/passwd\n") == "etcpasswd"
    assert _safe_rid("") and len(_safe_rid("")) == 12


def test_constructor_failure_after_reserve_releases(tmp_path, monkeypatch):
    """Blocker 2: if the response constructor raises AFTER reserve, the lease
    must not leak. The route re-raises, so use a client that surfaces 500."""
    import voicebook_stream.app as appmod

    m = tmp_path / "s.wav"
    d = _wav(m)
    reg = VoiceRegistry({"sumi-v1": VoiceEntry("sumi-v1", m, "ref", d)})
    lease = OneFlightLease()
    client = TestClient(create_app(reg, FakeSynth(), lease), raise_server_exceptions=False)

    def boom(*a, **k):
        raise RuntimeError("constructor blew up")

    monkeypatch.setattr(appmod, "ReservationStreamingResponse", boom)
    r = client.post("/speak/stream", json={"voice_id": "sumi-v1", "text": "hi"})
    assert r.status_code == 500
    assert lease.locked is False, "lease leaked on constructor failure"


# --- review-4: prefetch must not leak the STARTED backend generator ----------


def test_prefetch_wrapper_closes_backend_when_never_iterated(env):
    """The unstarted-generator trap, one layer down. After prefetch the backend
    is STARTED; if the response never iterates the wrapper (response-start
    fails), the wrapper's close() must still close the backend."""
    from voicebook_stream.app import _PrependIter

    reg, synth, lease, _ = env
    r = reg.get("sumi-v1")
    backend = synth.synthesize_stream("hi", r.master_path, r.reference_transcript)
    first = next(iter(backend))  # backend STARTED
    body = _PrependIter(first, backend)  # wrapper, never iterated further
    res = lease.reserve()
    resp = ReservationStreamingResponse(
        res, body, request_id="rid", voice_id="sumi-v1", chars=2, start=0.0
    )
    with pytest.raises(ClientDisconnect):
        _drive(resp, ["raise"])  # response-start fails, body never iterated
    assert synth.closed is True, "STARTED backend generator leaked (wrapper close skipped)"
    assert lease.locked is False


def test_prefetch_iter_order_and_close_once(env):
    """Wrapper yields first-then-rest in order, and closing it closes the
    backend exactly once."""
    from voicebook_stream.app import _PrependIter

    reg, synth, lease, _ = env
    r = reg.get("sumi-v1")
    backend = synth.synthesize_stream("hi", r.master_path, r.reference_transcript)
    first = next(iter(backend))
    body = _PrependIter(first, backend)
    assert list(body) == [b"pcm0", b"pcm1", b"pcm2"]  # order, no loss, no dup
    body.close()
    body.close()  # idempotent, no double-close error
    assert synth.closed is True


def test_stream_backend_failure_on_first_chunk_is_502(tmp_path):
    """A backend that raises on the first chunk -> typed 502, released."""
    m = tmp_path / "s.wav"
    d = _wav(m)
    reg = VoiceRegistry({"sumi-v1": VoiceEntry("sumi-v1", m, "ref", d)})
    lease = OneFlightLease()
    synth = FakeSynth(fail=True)  # raises SynthesisError on first iteration
    client = TestClient(create_app(reg, synth, lease), raise_server_exceptions=False)
    r = client.post("/speak/stream", json={"voice_id": "sumi-v1", "text": "hi"})
    assert r.status_code == 502
    assert lease.locked is False


def test_unary_resolve_failure_logged_once(env, caplog):
    """404 must produce exactly one terminal correlation record, not two."""
    import logging

    reg, synth, lease, client = env
    with caplog.at_level(logging.INFO, logger="voicebook.stream"):
        client.post("/speak", json={"voice_id": "nobody", "text": "hi"})
    records = [r for r in caplog.records if "404" in r.getMessage()]
    assert len(records) == 1, f"expected one 404 log, got {len(records)}"


# --- review-5: production runs ASGI spec_version 2.3, not 2.4 -----------------


def _drive_23(resp, *, disconnect_after_chunk: bool):
    """Drive __call__ through the ASGI 2.3 task-group branch that uvicorn HTTP
    actually uses. Disconnect is delivered via receive() (http.disconnect),
    and __call__ returns NORMALLY — not via an OSError from send()."""
    import asyncio

    chunk_sent = asyncio.Event()
    disconnect_now = asyncio.Event()

    async def receive():
        if disconnect_after_chunk:
            await chunk_sent.wait()  # let one body chunk go out first
        disconnect_now.set()
        return {"type": "http.disconnect"}

    async def send(msg):
        if msg["type"] == "http.response.body" and msg.get("body"):
            chunk_sent.set()
            if not disconnect_after_chunk:
                # disconnect-before-chunk: block body until disconnect fires,
                # so the stream is cancelled before delivering audio
                await disconnect_now.wait()

    return asyncio.get_event_loop().run_until_complete(
        resp({"type": "http", "asgi": {"spec_version": "2.3"}}, receive, send)
    )


def test_23_disconnect_before_chunk_closes_backend_and_logs_disconnect(env, caplog):
    import logging

    reg, synth, lease, _ = env
    r = reg.get("sumi-v1")
    backend = synth.synthesize_stream("hi", r.master_path, r.reference_transcript)
    first = next(iter(backend))
    from voicebook_stream.app import _PrependIter

    body = _PrependIter(first, backend)
    res = lease.reserve()
    resp = ReservationStreamingResponse(
        res, body, request_id="rid", voice_id="sumi-v1", chars=2, start=0.0
    )
    with caplog.at_level(logging.INFO, logger="voicebook.stream"):
        _drive_23(resp, disconnect_after_chunk=False)
    assert lease.locked is False
    assert synth.closed is True  # backend torn down on the 2.3 branch
    assert any("outcome=disconnect" in r.getMessage() for r in caplog.records), (
        "2.3 disconnect logged as ok, not disconnect"
    )


def test_23_disconnect_after_chunk_closes_backend_and_logs_disconnect(env, caplog):
    import logging

    reg, synth, lease, _ = env
    r = reg.get("sumi-v1")
    backend = synth.synthesize_stream("hi", r.master_path, r.reference_transcript)
    first = next(iter(backend))
    from voicebook_stream.app import _PrependIter

    body = _PrependIter(first, backend)
    res = lease.reserve()
    resp = ReservationStreamingResponse(
        res, body, request_id="rid", voice_id="sumi-v1", chars=2, start=0.0
    )
    with caplog.at_level(logging.INFO, logger="voicebook.stream"):
        _drive_23(resp, disconnect_after_chunk=True)
    assert lease.locked is False
    assert synth.closed is True
    assert any("outcome=disconnect" in r.getMessage() for r in caplog.records)


def test_teardown_close_failure_logs_teardown_outcome(env, caplog):
    """If body.close() raises, the record must say teardown:X, not ok — while
    still releasing the lease."""
    import logging

    _reg, _s, lease, _ = env
    body = _ExplodingCloseIter()  # yields "one", close() raises
    res = lease.reserve()
    resp = ReservationStreamingResponse(
        res, body, request_id="rid", voice_id="sumi-v1", chars=2, start=0.0
    )
    with caplog.at_level(logging.INFO, logger="voicebook.stream"):
        with pytest.raises(RuntimeError, match="teardown blew up"):
            _drive(resp, [])
    assert lease.locked is False
    assert any("outcome=teardown:RuntimeError" in r.getMessage() for r in caplog.records), (
        "teardown failure logged as success"
    )
