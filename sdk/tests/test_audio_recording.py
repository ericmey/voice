from __future__ import annotations

import asyncio
import stat
from types import SimpleNamespace
from unittest.mock import MagicMock

from sdk import audio_recording


def test_recording_dirs_default_under_voice_logs(monkeypatch, tmp_path) -> None:
    monkeypatch.delenv("LIVEKIT_EGRESS_HOST_RECORDINGS_DIR", raising=False)
    monkeypatch.setenv("LIVEKIT_VOICE_LOGS", str(tmp_path / "voice"))

    assert audio_recording._recordings_host_dir() == tmp_path / "voice" / "recordings"


def test_recording_dir_is_group_writable_for_egress(monkeypatch, tmp_path) -> None:
    """The root worker must not leave egress uid=1001,gid=0 a read-only bind."""
    root = tmp_path / "recordings"
    existing = root / "phone-sumi"
    existing.mkdir(parents=True, mode=0o755)
    existing.chmod(0o2755)  # exact live failure from SCL_CUS2NHcPcjmm
    monkeypatch.setenv("LIVEKIT_EGRESS_HOST_RECORDINGS_DIR", str(root))

    prepared = audio_recording._prepare_agent_recording_dir("phone-sumi")

    assert prepared == existing
    assert stat.S_IMODE(prepared.stat().st_mode) == 0o2775


def test_enabled_when_env_true(monkeypatch) -> None:
    monkeypatch.setenv("VOICE_RECORD_AUDIO", "true")
    assert audio_recording._enabled() is True


def test_enabled_default_false(monkeypatch) -> None:
    monkeypatch.delenv("VOICE_RECORD_AUDIO", raising=False)
    assert audio_recording._enabled() is False


def test_enabled_legacy_alias_no_longer_honored(monkeypatch) -> None:
    """LANGSMITH_ATTACH_AUDIO was retired alongside the OTel refactor.
    Operators must use VOICE_RECORD_AUDIO; the old alias is silently
    ignored so a stale .env file can't accidentally re-enable recording."""
    monkeypatch.delenv("VOICE_RECORD_AUDIO", raising=False)
    monkeypatch.setenv("LANGSMITH_ATTACH_AUDIO", "true")
    assert audio_recording._enabled() is False


def test_public_audio_url_when_base_set(monkeypatch, tmp_path) -> None:
    monkeypatch.setenv("VOICE_AUDIO_PUBLIC_BASE_URL", "https://media.example/recordings/")
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
    monkeypatch.delenv("VOICE_AUDIO_PUBLIC_BASE_URL", raising=False)
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


def test_annotate_call_audio_recording_writes_otel_attrs(monkeypatch, tmp_path) -> None:
    """Agent entrypoints decorate the active session span after session.start()."""
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

    audio_recording.annotate_call_audio_recording(rec)

    set_attrs = {call.args[0]: call.args[1] for call in span.set_attribute.call_args_list}
    assert set_attrs["voice.audio.call_sid"] == "SCL_call"
    assert set_attrs["voice.audio.path"] == str(audio_file)
    assert set_attrs["voice.audio.mime_type"] == "audio/ogg"
    assert set_attrs["voice.audio.egress_id"] == "EG_1"
    assert "voice.audio.bytes" not in set_attrs


def test_finalize_call_audio_recording_does_not_mutate_span(monkeypatch, tmp_path) -> None:
    """Shutdown finalization runs after LiveKit closes the session span."""
    audio_file = tmp_path / "nyla" / "SCL_call.ogg"
    audio_file.parent.mkdir(parents=True)
    audio_file.write_bytes(b"ogg data 12345")

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

    async def fake_stop(_recording):
        return None

    async def fake_wait(_path, _timeout_seconds):
        return True

    annotate = MagicMock()
    monkeypatch.setattr(audio_recording, "_stop_egress", fake_stop)
    monkeypatch.setattr(audio_recording, "_wait_for_recording", fake_wait)
    monkeypatch.setattr(audio_recording, "_annotate_active_span", annotate)

    loop = asyncio.new_event_loop()
    try:
        loop.run_until_complete(audio_recording.finalize_call_audio_recording(rec))
    finally:
        loop.close()

    annotate.assert_not_called()


def test_langsmith_alias_is_gone() -> None:
    """LangSmith is decommissioned. The back-compat alias claimed "agents
    currently call this name" and no agent ever did."""
    assert not hasattr(audio_recording, "attach_call_audio_to_langsmith")


def _recording(tmp_path):
    return audio_recording.CallAudioRecording(
        call_sid="SCL_views",
        agent_name="phone-sumi",
        room_name="phone-room",
        egress_id="EG_composite",
        host_path=tmp_path / "phone-sumi" / "SCL_views.ogg",
        container_path="/recordings/phone-sumi/SCL_views.ogg",
        mime_type="audio/ogg",
        started_at=0.0,
    )


def test_perspective_paths_are_call_sid_scoped(monkeypatch, tmp_path) -> None:
    monkeypatch.setenv("LIVEKIT_EGRESS_AUDIO_EXTENSION", "ogg")
    rec = _recording(tmp_path)

    host, container = audio_recording._perspective_paths(rec, "mic")

    assert host == tmp_path / "phone-sumi" / "SCL_views.mic.ogg"
    assert container == "/recordings/phone-sumi/SCL_views.mic.ogg"


def test_track_finders_keep_sip_input_separate_from_local_output() -> None:
    audio_kind = "kind_audio"
    mic_pub = SimpleNamespace(kind=audio_kind, sid="TR_MIC")
    tts_pub = SimpleNamespace(kind=audio_kind, sid="TR_TTS")
    room = SimpleNamespace(
        remote_participants={
            "egress": SimpleNamespace(
                identity="EG_1", track_publications={"wrong": tts_pub}
            ),
            "caller": SimpleNamespace(
                identity="sip_+1317", track_publications={"mic": mic_pub}
            ),
        },
        local_participant=SimpleNamespace(track_publications={"tts": tts_pub}),
    )

    assert audio_recording._find_sip_microphone_track_sid(room) == "TR_MIC"
    assert audio_recording._find_local_audio_track_sid(room) == "TR_TTS"


def test_track_egress_request_records_exact_track(monkeypatch, tmp_path) -> None:
    from livekit import api

    seen = []

    class FakeEgress:
        async def start_track_egress(self, request):
            seen.append(request)
            return SimpleNamespace(egress_id="EG_MIC")

    class FakeAPI:
        def __init__(self):
            self.egress = FakeEgress()

        async def aclose(self):
            return None

    monkeypatch.setattr(api, "LiveKitAPI", FakeAPI)
    rec = _recording(tmp_path)

    asyncio.run(
        audio_recording._start_track_egress(
            rec,
            perspective="mic",
            track_sid="TR_MIC",
        )
    )

    assert len(seen) == 1
    assert seen[0].room_name == "phone-room"
    assert seen[0].track_id == "TR_MIC"
    assert seen[0].file.filepath == "/recordings/phone-sumi/SCL_views.mic.ogg"
    assert rec.track_recordings["mic"].egress_id == "EG_MIC"


def test_one_track_start_failure_does_not_discard_other_views(monkeypatch, tmp_path) -> None:
    from livekit import api

    class FakeEgress:
        async def start_track_egress(self, _request):
            raise RuntimeError("track unavailable")

    class FakeAPI:
        def __init__(self):
            self.egress = FakeEgress()

        async def aclose(self):
            return None

    monkeypatch.setattr(api, "LiveKitAPI", FakeAPI)
    rec = _recording(tmp_path)

    asyncio.run(
        audio_recording._start_track_egress(
            rec,
            perspective="tts",
            track_sid="TR_TTS",
        )
    )

    assert rec.egress_id == "EG_composite", "composite evidence was discarded"
    assert rec.track_recordings == {}
    assert "track unavailable" in rec.start_errors["tts"]


def test_finalize_reports_missing_perspective_instead_of_claiming_complete(
    monkeypatch, tmp_path
) -> None:
    rec = _recording(tmp_path)
    rec.host_path.parent.mkdir(parents=True)
    rec.host_path.write_bytes(b"composite")

    async def fake_stop(_recording):
        return None

    async def fake_wait(_path, _timeout_seconds):
        return True

    monkeypatch.setattr(audio_recording, "_stop_egress", fake_stop)
    monkeypatch.setattr(audio_recording, "_wait_for_recording", fake_wait)

    asyncio.run(audio_recording.finalize_call_audio_recording(rec))

    assert rec.start_errors == {"mic": "track_not_found", "tts": "track_not_found"}
