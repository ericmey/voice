from __future__ import annotations

from unittest.mock import MagicMock

from sdk import audio_recording


def test_recording_dirs_default_under_voice_logs(monkeypatch, tmp_path) -> None:
    monkeypatch.delenv("LIVEKIT_EGRESS_HOST_RECORDINGS_DIR", raising=False)
    monkeypatch.setenv("LIVEKIT_VOICE_LOGS", str(tmp_path / "voice"))

    assert audio_recording._recordings_host_dir() == tmp_path / "voice" / "recordings"


def test_enabled_when_env_true(monkeypatch) -> None:
    monkeypatch.setenv("OPENCLAW_RECORD_AUDIO", "true")
    assert audio_recording._enabled() is True


def test_enabled_default_false(monkeypatch) -> None:
    monkeypatch.delenv("OPENCLAW_RECORD_AUDIO", raising=False)
    assert audio_recording._enabled() is False


def test_enabled_legacy_alias_no_longer_honored(monkeypatch) -> None:
    """LANGSMITH_ATTACH_AUDIO was retired alongside the SigNoz refactor.
    Operators must use OPENCLAW_RECORD_AUDIO; the old alias is silently
    ignored so a stale .env file can't accidentally re-enable recording."""
    monkeypatch.delenv("OPENCLAW_RECORD_AUDIO", raising=False)
    monkeypatch.setenv("LANGSMITH_ATTACH_AUDIO", "true")
    assert audio_recording._enabled() is False


def test_public_audio_url_when_base_set(monkeypatch, tmp_path) -> None:
    monkeypatch.setenv("OPENCLAW_AUDIO_PUBLIC_BASE_URL", "https://media.example/recordings/")
    rec = audio_recording.CallAudioRecording(
        call_sid="SCL_1",
        agent_name="nyla",
        room_name="r",
        egress_id="EG_1",
        host_path=tmp_path / "nyla" / "SCL_1.ogg",
        container_path="/recordings/nyla/SCL_1.ogg",
        mime_type="audio/ogg",
        started_at=0.0,
    )
    assert (
        audio_recording._public_audio_url(rec) == "https://media.example/recordings/nyla/SCL_1.ogg"
    )


def test_public_audio_url_returns_none_without_base(monkeypatch, tmp_path) -> None:
    monkeypatch.delenv("OPENCLAW_AUDIO_PUBLIC_BASE_URL", raising=False)
    rec = audio_recording.CallAudioRecording(
        call_sid="SCL_1",
        agent_name="nyla",
        room_name="r",
        egress_id=None,
        host_path=tmp_path / "nyla" / "SCL_1.ogg",
        container_path="/recordings/nyla/SCL_1.ogg",
        mime_type="audio/ogg",
        started_at=0.0,
    )
    assert audio_recording._public_audio_url(rec) is None


def test_annotate_active_span_writes_otel_attrs(monkeypatch, tmp_path) -> None:
    """Finalize path decorates the current OTel span with audio metadata."""
    audio_file = tmp_path / "nyla" / "SCL_call.ogg"
    audio_file.parent.mkdir(parents=True)
    audio_file.write_bytes(b"ogg data 12345")

    span = MagicMock()
    span.is_recording.return_value = True
    fake_trace = MagicMock()
    fake_trace.get_current_span.return_value = span
    monkeypatch.setattr("opentelemetry.trace.get_current_span", fake_trace.get_current_span)

    rec = audio_recording.CallAudioRecording(
        call_sid="SCL_call",
        agent_name="nyla",
        room_name="r",
        egress_id="EG_1",
        host_path=audio_file,
        container_path="/recordings/nyla/SCL_call.ogg",
        mime_type="audio/ogg",
        started_at=0.0,
    )

    audio_recording._annotate_active_span(rec, finalized=True)

    set_attrs = {call.args[0]: call.args[1] for call in span.set_attribute.call_args_list}
    assert set_attrs["openclaw.audio.call_sid"] == "SCL_call"
    assert set_attrs["openclaw.audio.path"] == str(audio_file)
    assert set_attrs["openclaw.audio.mime_type"] == "audio/ogg"
    assert set_attrs["openclaw.audio.egress_id"] == "EG_1"
    assert set_attrs["openclaw.audio.bytes"] == len(b"ogg data 12345")


def test_legacy_alias_still_imports() -> None:
    """Agents that still call ``attach_call_audio_to_langsmith`` keep working."""
    assert (
        audio_recording.attach_call_audio_to_langsmith
        is audio_recording.finalize_call_audio_recording
    )
