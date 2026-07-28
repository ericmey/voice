"""/v1/audio/speech — the OpenAI request-shape alias.

The whole claim of this route is that it is a TRANSLATION and nothing else:
below the field renaming it runs the same code /speak runs, so it cannot drift
away from the readiness / char-limit / registry / lease / synthesis / error
contracts. These tests hold it to that, and to refusing what it cannot honour
instead of quietly returning something different from what was asked for.
"""

from __future__ import annotations

import hashlib
import wave
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from voicebook_stream.app import MAX_INPUT_CHARS, create_app
from voicebook_stream.lease import OneFlightLease
from voicebook_stream.registry import VoiceEntry, VoiceRegistry
from voicebook_stream.synth import SynthesisError

WAV = b"RIFF" + b"\x00" * 64


def _master(p: Path) -> str:
    with wave.open(str(p), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(24000)
        w.writeframes(b"\x01\x00" * 2400)
    return hashlib.sha256(p.read_bytes()).hexdigest()


class FakeSynth:
    ready = True

    def __init__(self, *, fail=False, empty=False):
        self.fail, self.empty = fail, empty
        self.calls: list[tuple] = []

    def synthesize(self, text, master_path, reference_transcript):
        self.calls.append((text, master_path, reference_transcript))
        if self.fail:
            raise SynthesisError("backend exploded")
        return b"" if self.empty else WAV

    def synthesize_stream(self, text, master_path, reference_transcript):  # unused here
        raise AssertionError("the completed-response path must not stream")


@pytest.fixture
def env(tmp_path):
    m = tmp_path / "sumi.wav"
    reg = VoiceRegistry({"sumi-v1": VoiceEntry("sumi-v1", m, "ref line", _master(m))})
    lease = OneFlightLease()
    synth = FakeSynth()
    return reg, synth, lease, TestClient(create_app(reg, synth, lease))


def _speech(client, **body):
    payload = {"model": "voicebook", "input": "hello", "voice": "sumi-v1"}
    payload.update(body)
    return client.post("/v1/audio/speech", json=payload)


# --- the translation ----------------------------------------------------------


def test_input_and_voice_map_through_to_the_synthesizer(env):
    _, synth, lease, client = env
    r = _speech(client, input="say this exactly", voice="sumi-v1")
    assert r.status_code == 200
    assert r.headers["content-type"] == "audio/wav"
    assert r.content == WAV
    assert synth.calls[0][0] == "say this exactly"  # input -> text
    assert synth.calls[0][2] == "ref line"  # registry entry resolved from voice
    assert lease.locked is False


def test_model_is_metadata_and_does_not_dispatch(env):
    """SDKs require `model`. We have one engine -- accept any value, ignore it."""
    _, synth, _, client = env
    assert _speech(client, model="tts-1-hd").status_code == 200
    assert _speech(client, model="gpt-4o-mini-tts").status_code == 200
    assert len(synth.calls) == 2


def test_response_format_defaults_to_wav(env):
    _, _, _, client = env
    assert _speech(client).status_code == 200


def test_request_id_header_is_echoed(env):
    _, _, _, client = env
    r = client.post(
        "/v1/audio/speech",
        json={"input": "hi", "voice": "sumi-v1"},
        headers={"X-Request-ID": "abc-123"},
    )
    assert r.headers["X-Request-ID"] == "abc-123"


# --- refuse what we cannot honour --------------------------------------------


def test_unsupported_format_is_400_not_silently_wav(env):
    _, synth, lease, client = env
    r = _speech(client, response_format="mp3")
    assert r.status_code == 400
    assert "mp3" in r.json()["detail"]
    assert synth.calls == []  # refused BEFORE taking the lease
    assert lease.locked is False


def test_unsupported_speed_is_400_not_silently_1x(env):
    _, synth, lease, client = env
    r = _speech(client, speed=1.5)
    assert r.status_code == 400
    assert "1.0" in r.json()["detail"]
    assert synth.calls == []
    assert lease.locked is False


def test_missing_input_is_422(env):
    _, _, _, client = env
    assert client.post("/v1/audio/speech", json={"voice": "sumi-v1"}).status_code == 422


def test_missing_voice_is_422(env):
    _, _, _, client = env
    assert client.post("/v1/audio/speech", json={"input": "hi"}).status_code == 422


def test_empty_input_is_422(env):
    _, _, _, client = env
    assert _speech(client, input="").status_code == 422


# --- the shared contracts, reached through the alias --------------------------


def test_char_limit_contract_is_shared(env):
    _, _, lease, client = env
    assert _speech(client, input="x" * MAX_INPUT_CHARS).status_code == 200
    r = _speech(client, input="x" * (MAX_INPUT_CHARS + 1))
    assert r.status_code == 413
    assert "never truncated" in r.json()["detail"]
    assert lease.locked is False


def test_unknown_voice_contract_is_shared(env):
    _, _, lease, client = env
    r = _speech(client, voice="nobody-v9")
    assert r.status_code == 404
    assert "nobody-v9" in r.json()["detail"]
    assert lease.locked is False


def test_readiness_contract_is_shared(tmp_path):
    m = tmp_path / "s.wav"
    reg = VoiceRegistry({"sumi-v1": VoiceEntry("sumi-v1", m, "r", _master(m))})
    synth = FakeSynth()
    synth.ready = False
    lease = OneFlightLease()
    client = TestClient(create_app(reg, synth, lease))
    assert _speech(client).status_code == 503
    assert lease.locked is False


def test_lease_contract_is_shared(env):
    """One-flight is fleet-wide, not per-route. A held lease must 429 the alias
    exactly as it 429s /speak -- otherwise the OpenAI surface is a second door
    around the concurrency limit the whole GPU budget depends on."""
    _, _, lease, client = env
    held = lease.reserve()
    try:
        r = _speech(client)
        assert r.status_code == 429
        assert "already in flight" in r.json()["detail"]
    finally:
        held.close()
    assert _speech(client).status_code == 200  # recovers once released


def test_synthesis_failure_contract_is_shared(tmp_path):
    m = tmp_path / "s.wav"
    reg = VoiceRegistry({"sumi-v1": VoiceEntry("sumi-v1", m, "r", _master(m))})
    lease = OneFlightLease()
    client = TestClient(create_app(reg, FakeSynth(fail=True), lease))
    r = _speech(client)
    assert r.status_code == 502
    assert "backend exploded" in r.json()["detail"]
    assert lease.locked is False


def test_empty_audio_is_502_never_a_silent_200(tmp_path):
    m = tmp_path / "s.wav"
    reg = VoiceRegistry({"sumi-v1": VoiceEntry("sumi-v1", m, "r", _master(m))})
    lease = OneFlightLease()
    client = TestClient(create_app(reg, FakeSynth(empty=True), lease))
    assert _speech(client).status_code == 502
    assert lease.locked is False


def test_alias_and_speak_are_the_same_path(env):
    """Same input through both doors must produce byte-identical audio and use
    the same registry entry -- the point of aliasing instead of reimplementing."""
    _, synth, _, client = env
    a = _speech(client, input="identical text", voice="sumi-v1")
    b = client.post("/speak", json={"voice_id": "sumi-v1", "text": "identical text"})
    assert a.status_code == b.status_code == 200
    assert a.content == b.content
    assert synth.calls[0] == synth.calls[1]


def test_alias_logs_under_its_own_kind(env, caplog):
    """Shared code, distinguishable records -- an operator must be able to tell
    which door a request came through."""
    import logging

    _, _, _, client = env
    with caplog.at_level(logging.INFO, logger="voicebook.stream"):
        _speech(client, input="hello")
        client.post("/speak", json={"voice_id": "sumi-v1", "text": "hello"})
    text = caplog.text
    assert "openai request_id=" in text
    assert "unary request_id=" in text
