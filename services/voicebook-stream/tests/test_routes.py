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
    assert client.get("/healthz").json()["ready"] is False
    synth.ready = True
    assert client.get("/healthz").json()["ready"] is True


# --- streaming happy path -----------------------------------------------------


def test_stream_returns_pcm_and_releases_after(env):
    reg, synth, lease, client = env
    with client.stream("POST", "/speak/stream", json={"voice_id": "sumi-v1", "text": "hi"}) as r:
        body = b"".join(r.iter_bytes())
    assert r.status_code == 200
    assert body == b"pcm0pcm1pcm2"
    assert r.headers["content-type"].startswith("audio/L16")
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


def test_lifecycle_normal_completion_releases(env):
    reg, synth, lease, _ = env
    r = reg.get("sumi-v1")
    res = lease.reserve()
    gen = synth.synthesize_stream("hi", r.master_path, r.reference_transcript)
    resp = ReservationStreamingResponse(res, gen, request_id="rid")
    _drive(resp, [])  # all sends succeed
    assert lease.locked is False and synth.closed is True


def test_lifecycle_disconnect_before_first_chunk_releases(env):
    reg, synth, lease, _ = env
    r = reg.get("sumi-v1")
    res = lease.reserve()
    gen = synth.synthesize_stream("hi", r.master_path, r.reference_transcript)
    resp = ReservationStreamingResponse(res, gen, request_id="rid")
    # send #1 is response.start -> raise: disconnect before any body chunk
    with pytest.raises(ClientDisconnect):
        _drive(resp, ["raise"])
    assert lease.locked is False, "lease leaked when disconnect hit before first chunk"


def test_lifecycle_disconnect_after_chunk_releases(env):
    reg, synth, lease, _ = env
    r = reg.get("sumi-v1")
    res = lease.reserve()
    gen = synth.synthesize_stream("hi", r.master_path, r.reference_transcript)
    resp = ReservationStreamingResponse(res, gen, request_id="rid")
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
    resp = ReservationStreamingResponse(res, gen, request_id="rid")
    with pytest.raises(SynthesisError):
        _drive(resp, [])  # sends fine, but the generator raises on iteration
    assert lease.locked is False


def test_active_collision_returns_429(env):
    """Second request while the lease is held mid-stream gets 429, not 200."""
    reg, synth, lease, client = env
    lease.reserve()  # simulate request 1 actively holding the lease
    r = client.post("/speak/stream", json={"voice_id": "sumi-v1", "text": "hi"})
    assert r.status_code == 429
