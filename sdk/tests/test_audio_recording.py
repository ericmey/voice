from __future__ import annotations

from pathlib import Path

from sdk import audio_recording


def test_recording_dirs_default_under_voice_logs(monkeypatch, tmp_path) -> None:
    monkeypatch.delenv("LIVEKIT_EGRESS_HOST_RECORDINGS_DIR", raising=False)
    monkeypatch.setenv("LIVEKIT_VOICE_LOGS", str(tmp_path / "voice"))

    assert audio_recording._recordings_host_dir() == tmp_path / "voice" / "recordings"


def test_langsmith_project_reads_otel_header(monkeypatch) -> None:
    monkeypatch.delenv("LANGSMITH_PROJECT", raising=False)
    monkeypatch.setenv("OTEL_EXPORTER_OTLP_HEADERS", "x-api-key=k,Langsmith-Project=Nyla")

    assert audio_recording._langsmith_project() == "Nyla"


def test_langsmith_audio_attachment_run_uses_call_thread(monkeypatch, tmp_path) -> None:
    audio_path = tmp_path / "call.ogg"
    audio_path.write_bytes(b"ogg data")
    created: dict[str, object] = {}

    class FakeRunTree:
        def __init__(self, **kwargs) -> None:
            created.update(kwargs)
            self.ended = False
            self.posted = False

        def end(self, **kwargs) -> None:
            created["end"] = kwargs

        def post(self) -> None:
            created["posted"] = True

        def wait(self) -> None:
            created["waited"] = True

    class FakeClient:
        def __init__(self, **kwargs) -> None:
            created["client"] = kwargs

    monkeypatch.setenv("LANGSMITH_PROJECT", "Nyla")
    monkeypatch.setenv("LANGSMITH_API_KEY", "test-key")
    monkeypatch.setattr("langsmith.Client", FakeClient)
    monkeypatch.setattr("langsmith.run_trees.RunTree", FakeRunTree)

    recording = audio_recording.CallAudioRecording(
        call_sid="SCL_call",
        agent_name="phone-nyla",
        room_name="room-1",
        egress_id="EG_1",
        host_path=Path(audio_path),
        container_path="/recordings/phone-nyla/SCL_call.ogg",
        mime_type="audio/ogg",
        started_at=0.0,
    )

    audio_recording._upload_langsmith_attachment(recording)

    assert created["name"] == "call_audio"
    assert created["project_name"] == "Nyla"
    assert created["attachments"] == {"full_call_audio": ("audio/ogg", audio_path)}
    assert created["extra"]["metadata"]["thread_id"] == "SCL_call"
    assert created["extra"]["metadata"]["recording_bytes"] == len(b"ogg data")
    assert created["posted"] is True
    assert created["waited"] is True
