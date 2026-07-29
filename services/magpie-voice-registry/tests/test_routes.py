from __future__ import annotations

import hashlib
import json
import wave
from pathlib import Path

import httpx
from fastapi.testclient import TestClient
from magpie_voice_registry.app import create_app
from magpie_voice_registry.pronunciation import PronunciationDictionary
from magpie_voice_registry.registry import load_registry


def app_for(tmp_path: Path, *, nim_status: int = 200):
    prompt = tmp_path / "sumi.wav"
    with wave.open(str(prompt), "wb") as wav:
        wav.setnchannels(1)
        wav.setsampwidth(2)
        wav.setframerate(22050)
        wav.writeframes(b"\0\0" * 22050 * 5)
    registry_path = tmp_path / "registry.json"
    registry_path.write_text(
        json.dumps(
            {
                "sumi-v1": {
                    "prompt_path": str(prompt),
                    "sha256": hashlib.sha256(prompt.read_bytes()).hexdigest(),
                    "quality": 40,
                }
            }
        )
    )

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/v1/health/ready":
            return httpx.Response(200, json={"status": "ready"})
        body = request.read()
        assert b'name="prompt_quality"' in body
        assert b"40" in body
        assert b'name="audio_prompt"' in body
        assert b'name="audio_prompt_transcript"' not in body
        assert b'name="custom_dictionary"' in body
        assert "Aoi  aʊi,Nyla  naɪlə".encode() in body
        return httpx.Response(
            nim_status, content=b"upstream failed" if nim_status != 200 else b"\x01\x00\x02\x00"
        )

    transport = httpx.MockTransport(handler)

    class MockClient(httpx.AsyncClient):
        def __init__(self, **kwargs):
            super().__init__(transport=transport, **kwargs)

    return create_app(
        load_registry(registry_path),
        "http://nim",
        pronunciations=PronunciationDictionary(
            entries={"Aoi": "aʊi", "Nyla": "naɪlə"}, sha256="dictionary-sha"
        ),
        client_factory=MockClient,
    )


def test_health_and_roster(tmp_path: Path) -> None:
    client = TestClient(app_for(tmp_path))
    health = client.get("/healthz").json()
    assert health["ready"] is True
    assert health["voices"] == ["sumi-v1"]
    assert health["pronunciations"] == 2
    assert health["pronunciation_sha256"] == "dictionary-sha"
    assert client.get("/voices").json()["voices"][0]["quality"] == 40


def test_stream_preserves_raw_pcm(tmp_path: Path) -> None:
    client = TestClient(app_for(tmp_path))
    response = client.post("/speak/stream", json={"voice_id": "sumi-v1", "text": "hello"})
    assert response.status_code == 200
    assert response.content == b"\x01\x00\x02\x00"
    assert response.headers["x-audio-sample-rate"] == "22050"


def test_completed_route_wraps_wav(tmp_path: Path) -> None:
    client = TestClient(app_for(tmp_path))
    response = client.post("/speak", json={"voice_id": "sumi-v1", "text": "hello"})
    assert response.status_code == 200
    assert response.content[:4] == b"RIFF"


def test_openai_speech_translates_to_completed_route(tmp_path: Path) -> None:
    client = TestClient(app_for(tmp_path))
    response = client.post(
        "/v1/audio/speech",
        json={
            "model": "magpie-tts-zeroshot",
            "input": "hello",
            "voice": "sumi-v1",
            "response_format": "wav",
        },
    )
    assert response.status_code == 200
    assert response.content[:4] == b"RIFF"


def test_openai_speech_refuses_unsupported_options(tmp_path: Path) -> None:
    client = TestClient(app_for(tmp_path))
    assert (
        client.post(
            "/v1/audio/speech",
            json={"input": "hello", "voice": "sumi-v1", "response_format": "mp3"},
        ).status_code
        == 400
    )
    assert (
        client.post(
            "/v1/audio/speech",
            json={"input": "hello", "voice": "sumi-v1", "speed": 1.5},
        ).status_code
        == 400
    )


def test_unknown_voice_fails_loud(tmp_path: Path) -> None:
    client = TestClient(app_for(tmp_path))
    response = client.post("/speak", json={"voice_id": "nobody", "text": "hello"})
    assert response.status_code == 404


def test_stream_reports_nim_failure_before_committing_200(tmp_path: Path) -> None:
    client = TestClient(app_for(tmp_path, nim_status=500))
    response = client.post("/speak/stream", json={"voice_id": "sumi-v1", "text": "hello"})
    assert response.status_code == 502
    assert "HTTP 500" in response.json()["detail"]
