"""Service tests. No GPU, no model — the synthesizer is injected.

Every test here corresponds to a failure mode we hit for real today.
"""

from __future__ import annotations

import hashlib
import threading
import wave
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from voicebook_tts import MAX_INPUT_CHARS, VoiceEntry, VoiceRegistry, create_app
from voicebook_tts.registry import RegistryError
from voicebook_tts.synth import SynthesisError


def _wav(path: Path, seconds: float = 0.2, rate: int = 24000) -> str:
    with wave.open(str(path), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(rate)
        w.writeframes(b"\x01\x00" * int(rate * seconds))
    return hashlib.sha256(path.read_bytes()).hexdigest()


class FakeSynth:
    def __init__(self, fail: bool = False) -> None:
        self.fail = fail
        self.calls: list[tuple[str, str]] = []

    def speak(self, text: str, master_path: Path, reference_transcript: str) -> bytes:
        if self.fail:
            raise SynthesisError("backend exploded")
        self.calls.append((text, reference_transcript))
        return b"RIFF" + b"\x00" * 64


@pytest.fixture
def env(tmp_path: Path):
    master = tmp_path / "nyla.wav"
    digest = _wav(master)
    reg = VoiceRegistry(
        {"nyla-v1": VoiceEntry("nyla-v1", master, "the reference transcript", digest)}
    )
    synth = FakeSynth()
    return reg, synth, master, TestClient(create_app(reg, synth))


def test_startup_verification_passes(env):
    reg, *_ = env
    reg.verify_all()


def test_speak_returns_wav_and_passes_server_side_transcript(env):
    _, synth, _, client = env
    r = client.post("/speak", json={"voice_id": "nyla-v1", "text": "morning"})
    assert r.status_code == 200
    assert r.headers["content-type"] == "audio/wav"
    # The transcript came from the registry, not the caller.
    assert synth.calls == [("morning", "the reference transcript")]


def test_unknown_voice_fails_loudly(env):
    *_, client = env
    r = client.post("/speak", json={"voice_id": "nobody", "text": "hi"})
    assert r.status_code == 404
    assert "unknown voice_id" in r.json()["detail"]


def test_over_limit_is_refused_not_truncated(env):
    *_, client = env
    r = client.post("/speak", json={"voice_id": "nyla-v1", "text": "x" * (MAX_INPUT_CHARS + 1)})
    assert r.status_code == 413
    assert "truncated" in r.json()["detail"]


def test_swapped_master_is_detected_before_speaking(env):
    """Startup verification is not a permanent promise. A master replaced while
    the service is resident must be caught on the NEXT request."""
    _, synth, master, client = env
    master.write_bytes(master.read_bytes() + b"tampered")
    r = client.post("/speak", json={"voice_id": "nyla-v1", "text": "hi"})
    assert r.status_code == 500
    assert "hash mismatch" in r.json()["detail"]
    assert synth.calls == []  # never spoke


def test_missing_master_is_detected(env):
    _, synth, master, client = env
    master.unlink()
    r = client.post("/speak", json={"voice_id": "nyla-v1", "text": "hi"})
    assert r.status_code == 500
    assert synth.calls == []


def test_backend_failure_returns_no_audio(tmp_path: Path):
    master = tmp_path / "m.wav"
    digest = _wav(master)
    reg = VoiceRegistry({"x": VoiceEntry("x", master, "t", digest)})
    client = TestClient(create_app(reg, FakeSynth(fail=True)))
    r = client.post("/speak", json={"voice_id": "x", "text": "hi"})
    assert r.status_code == 502
    assert r.content != b"RIFF"


def test_empty_registry_refuses_to_start(tmp_path: Path):
    with pytest.raises(RegistryError, match="empty"):
        VoiceRegistry({}).verify_all()


class BlockingSynth:
    """Holds the generation lock until released, so a second request can be
    observed hitting the concurrency guard rather than queueing invisibly."""

    def __init__(self) -> None:
        self.entered = threading.Event()
        self.release = threading.Event()

    def speak(self, text: str, master_path: Path, reference_transcript: str) -> bytes:
        self.entered.set()
        self.release.wait(timeout=5)
        return b"RIFF" + b"\x00" * 64


def test_second_concurrent_request_is_refused_not_queued(tmp_path: Path):
    """The 429 guard was previously claimed but untested."""
    master = tmp_path / "m.wav"
    digest = _wav(master)
    reg = VoiceRegistry({"x": VoiceEntry("x", master, "t", digest)})
    synth = BlockingSynth()
    client = TestClient(create_app(reg, synth))

    first: dict[str, int] = {}

    def run_first():
        first["status"] = client.post("/speak", json={"voice_id": "x", "text": "a"}).status_code

    t = threading.Thread(target=run_first)
    t.start()
    assert synth.entered.wait(timeout=5), "first request never reached the backend"

    # Second request arrives while the first still holds the lock.
    second = client.post("/speak", json={"voice_id": "x", "text": "b"})
    assert second.status_code == 429
    assert "concurrency is unproven" in second.json()["detail"]

    synth.release.set()
    t.join(timeout=5)
    assert first["status"] == 200


def test_real_backend_wraps_arbitrary_exceptions_as_typed_502(tmp_path: Path):
    """A backend raising something OTHER than SynthesisError must still produce
    the promised typed 502, never an untyped 500. The fake synthesizer used
    elsewhere raises the correct type and so could never catch this."""
    master = tmp_path / "m.wav"
    digest = _wav(master)

    class ExplodingSynth:
        def speak(self, text, master_path, reference_transcript):
            raise RuntimeError("CUDA out of memory")

    reg = VoiceRegistry({"x": VoiceEntry("x", master, "t", digest)})
    client = TestClient(create_app(reg, ExplodingSynth()), raise_server_exceptions=False)
    r = client.post("/speak", json={"voice_id": "x", "text": "hi"})
    assert r.status_code == 502, f"got {r.status_code}: arbitrary backend errors must be typed"
