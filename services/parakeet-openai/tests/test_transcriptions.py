"""Route + normalization proofs for the transcription shim. No GPU, no Riva.

The backend is injected, so these tests prove the SEAM: what shapes are refused,
what reaches the recognizer, and that a failure can never present as success.
"""

from __future__ import annotations

import io
import math
import struct
import wave

import pytest
from fastapi.testclient import TestClient
from parakeet_openai.app import create_app
from parakeet_openai.asr import AsrError, Transcript
from parakeet_openai.audio import AudioError, to_riva_pcm


def _wav_bytes(rate=16000, channels=1, seconds=1.0, freq=440) -> bytes:
    """A real sine in a real wav container. Built with struct, not audioop --
    audioop is removed in 3.13 and the service no longer depends on it."""
    frames = int(rate * seconds)
    samples = [int(10000 * math.sin(2 * math.pi * freq * i / rate)) for i in range(frames)]
    pcm = b"".join(struct.pack("<" + "h" * channels, *([s] * channels)) for s in samples)
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(channels)
        w.setsampwidth(2)
        w.setframerate(rate)
        w.writeframes(pcm)
    return buf.getvalue()


class FakeBackend:
    uri = "fake:50051"
    language = "en-US"

    def __init__(
        self,
        *,
        text="hello there",
        fail=False,
        construct_fail=False,
        ready=True,
    ):
        if construct_fail:
            raise RuntimeError("no riva client")
        self.text, self.fail, self.ready = text, fail, ready
        self.seen: bytes | None = None

    def check_ready(self) -> None:
        if not self.ready:
            raise AsrError("backend channel unavailable")

    def transcribe(self, pcm: bytes) -> Transcript:
        self.seen = pcm
        if self.fail:
            raise AsrError("backend exploded")
        return Transcript(text=self.text, duration_s=len(pcm) / 32000)


@pytest.fixture
def env():
    backend = FakeBackend()
    client = TestClient(create_app(lambda: backend))
    return backend, client


def _post(client, data: bytes, **form):
    return client.post(
        "/v1/audio/transcriptions",
        files={"file": ("clip.wav", data, "audio/wav")},
        data=form,
    )


# --- the happy path ----------------------------------------------------------


def test_json_is_the_openai_shape(env):
    backend, client = env
    r = _post(client, _wav_bytes())
    assert r.status_code == 200
    assert r.json() == {"text": "hello there"}
    assert r.headers["X-Request-ID"]


def test_text_format_returns_bare_text(env):
    _, client = env
    r = _post(client, _wav_bytes(), response_format="text")
    assert r.status_code == 200
    assert r.text == "hello there"


def test_verbose_json_reports_measured_duration(env):
    _, client = env
    r = _post(client, _wav_bytes(seconds=2.0), response_format="verbose_json")
    body = r.json()
    assert body["text"] == "hello there"
    assert body["duration"] == pytest.approx(2.0, abs=0.05)


def test_model_field_is_accepted_and_ignored(env):
    """SDKs require `model`. We have one engine -- accept it, never dispatch."""
    _, client = env
    assert _post(client, _wav_bytes(), model="whisper-1").status_code == 200


# --- what must NEVER be transcribed best-effort ------------------------------


def test_resamples_before_the_recognizer_sees_it(env):
    """A 48k stereo upload must reach Riva as 16k mono, or it returns fluent
    nonsense. This is the defect the whole audio module exists to prevent."""
    backend, client = env
    assert _post(client, _wav_bytes(rate=48000, channels=2, seconds=1.0)).status_code == 200
    assert len(backend.seen) == pytest.approx(32000, rel=0.02)  # 1s of 16k s16 mono


def test_undecodable_bytes_are_400_not_a_transcript(env):
    _, client = env
    r = _post(client, b"this is not audio at all, not even close")
    assert r.status_code == 400
    assert "decode" in r.json()["detail"].lower() or "ffmpeg" in r.json()["detail"].lower()


def test_empty_upload_is_400(env):
    _, client = env
    assert _post(client, b"").status_code == 400


def test_oversize_upload_is_413_never_truncated(env):
    _, client = env
    r = _post(client, b"RIFF" + b"\x00" * (26 * 1024 * 1024))
    assert r.status_code == 413
    assert "truncated" in r.json()["detail"]


def test_unsupported_response_format_is_400(env):
    _, client = env
    r = _post(client, _wav_bytes(), response_format="srt")
    assert r.status_code == 400
    assert "srt" in r.json()["detail"]


# --- failure must never look like success ------------------------------------


def test_backend_error_is_502_not_empty_text():
    backend = FakeBackend(fail=True)
    client = TestClient(create_app(lambda: backend))
    r = _post(client, _wav_bytes())
    assert r.status_code == 502
    assert "backend exploded" in r.json()["detail"]


def test_genuine_silence_returns_empty_text_at_200():
    """Empty is a legitimate answer and is reported plainly. It is reachable
    ONLY on success -- backend failure took the 502 branch above."""
    client = TestClient(create_app(lambda: FakeBackend(text="")))
    r = _post(client, _wav_bytes())
    assert r.status_code == 200
    assert r.json() == {"text": ""}


def test_healthz_is_red_when_the_backend_cannot_be_built():
    def broken():
        raise RuntimeError("nvidia-riva-client is not installed")

    client = TestClient(create_app(broken))
    r = client.get("/healthz")
    assert r.status_code == 503  # docker healthcheck / curl -f must fail
    assert r.json()["ready"] is False
    assert "riva" in r.json()["error"]


def test_healthz_is_red_when_lazy_client_exists_but_backend_is_down():
    """Client construction is not readiness: gRPC channels connect lazily."""
    client = TestClient(create_app(lambda: FakeBackend(ready=False)))
    r = client.get("/healthz")
    assert r.status_code == 503
    assert r.json()["ready"] is False
    assert "backend channel unavailable" in r.json()["error"]


def test_transcribe_is_503_when_lazy_client_exists_but_backend_is_down():
    client = TestClient(create_app(lambda: FakeBackend(ready=False)))
    r = _post(client, _wav_bytes())
    assert r.status_code == 503
    assert "backend channel unavailable" in r.json()["detail"]


def test_transcribe_is_503_when_the_backend_cannot_be_built():
    def broken():
        raise RuntimeError("connection refused")

    client = TestClient(create_app(broken))
    assert _post(client, _wav_bytes()).status_code == 503


# --- normalization unit level ------------------------------------------------


def test_to_riva_pcm_rejects_empty():
    with pytest.raises(AudioError):
        to_riva_pcm(b"")


def test_to_riva_pcm_is_idempotent_on_target_shape():
    pcm, secs = to_riva_pcm(_wav_bytes(rate=16000, channels=1, seconds=1.0))
    assert len(pcm) == 32000
    assert secs == pytest.approx(1.0, abs=0.01)


def test_to_riva_pcm_downmixes_stereo():
    pcm, _ = to_riva_pcm(_wav_bytes(rate=16000, channels=2, seconds=1.0))
    assert len(pcm) == 32000  # halved, not doubled


def test_target_shape_wav_skips_ffmpeg_entirely(monkeypatch):
    """The fast path must be a real fast path -- if it silently fell through to
    ffmpeg, a container without ffmpeg would fail on input it can serve."""
    import parakeet_openai.audio as audio_mod

    monkeypatch.setattr(audio_mod, "find_ffmpeg", lambda: None)
    pcm, secs = audio_mod.to_riva_pcm(_wav_bytes(rate=16000, channels=1, seconds=0.5))
    assert len(pcm) == 16000
    assert secs == pytest.approx(0.5, abs=0.01)


def test_decode_error_drops_inherited_grpc_log_noise():
    """grpc's fork handler writes glog lines to the inherited fd 2, which land
    in ffmpeg's stderr. Left alone they become the first 300 chars of the error,
    so someone debugging a corrupt upload reads gRPC internals instead of the
    actual reason."""
    from parakeet_openai.audio import _ffmpeg_reason

    stderr = (
        b"I0728 18:39:38.146224 2465799 ev_poll_posix.cc:593] FD from fork parent "
        b"still in poll list: fd(17, generation: 1)\n"
        b"[in#0 @ 0xc0cc10000] Error opening input: Invalid data found when processing input\n"
    )
    reason = _ffmpeg_reason(stderr)
    assert "Invalid data found" in reason
    assert "fork parent" not in reason
    assert "ev_poll_posix" not in reason


def test_missing_ffmpeg_is_a_clear_error_not_a_crash(monkeypatch):
    import parakeet_openai.audio as audio_mod

    monkeypatch.setattr(audio_mod, "find_ffmpeg", lambda: None)
    with pytest.raises(AudioError, match="ffmpeg was not found"):
        audio_mod.to_riva_pcm(_wav_bytes(rate=48000, channels=2))
