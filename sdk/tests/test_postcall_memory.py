"""Tests for sdk.postcall_memory — the post-call extraction module."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from sdk.postcall_memory import (
    CATEGORIES,
    ExtractedMemory,
    _capture_one,
    _validate_memory,
    run_extraction,
)

from sdk import postcall_memory

# --- _validate_memory -------------------------------------------------------


def test_validate_memory_happy_path() -> None:
    raw = {
        "content": "Eric mentioned he's anxious about the migration cost.",
        "summary": "Migration cost anxiety",
        "topics": ["migration", "qdrant"],
        "category": "project",
    }
    m = _validate_memory(raw)
    assert m is not None
    assert m.content == "Eric mentioned he's anxious about the migration cost."
    assert m.summary == "Migration cost anxiety"
    assert m.topics == ["migration", "qdrant"]
    assert m.category == "project"


def test_validate_memory_missing_content_returns_none() -> None:
    assert _validate_memory({"summary": "x"}) is None
    assert _validate_memory({"content": "  "}) is None


def test_validate_memory_unknown_category_falls_back_to_general() -> None:
    raw = {"content": "hello", "category": "narnia"}
    m = _validate_memory(raw)
    assert m is not None
    assert m.category == "general"


def test_validate_memory_caps_topics_at_five_lowercases() -> None:
    raw = {
        "content": "x",
        "topics": ["A", "B", "C", "D", "E", "F", "G"],
    }
    m = _validate_memory(raw)
    assert m is not None
    assert m.topics == ["a", "b", "c", "d", "e"]


def test_validate_memory_drops_non_dict() -> None:
    assert _validate_memory("nope") is None
    assert _validate_memory(None) is None
    assert _validate_memory(["a", "b"]) is None


def test_validate_memory_default_summary_from_content() -> None:
    raw = {"content": "hello world"}
    m = _validate_memory(raw)
    assert m is not None
    assert m.summary == "hello world"


def test_categories_includes_general_fallback() -> None:
    assert "general" in CATEGORIES


# --- _extract_memories (full extraction with mocked Gemini) -----------------


@pytest.mark.asyncio
async def test_extract_memories_happy_path(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("GEMINI_API_KEY", "fake-key")

    fake_response_text = json.dumps(
        {
            "memories": [
                {
                    "content": "Eric mentioned he wants to look at Vespa",
                    "summary": "Vespa research interest",
                    "topics": ["vespa", "embedding-databases"],
                    "category": "idea",
                },
                {
                    "content": "Eric is planning to bring Bridget into the budget call",
                    "summary": "Budget call with Bridget",
                    "topics": ["budget", "bridget"],
                    "category": "household",
                },
            ]
        }
    )

    mock_response = MagicMock()
    mock_response.text = fake_response_text
    mock_models = MagicMock()
    mock_models.generate_content.return_value = mock_response
    mock_client = MagicMock()
    mock_client.models = mock_models

    with patch("sdk.postcall_memory.genai.Client", return_value=mock_client):
        result = await postcall_memory._extract_memories("transcript content here")

    assert result.status == "extracted"
    assert len(result.memories) == 2
    assert result.memories[0].content == "Eric mentioned he wants to look at Vespa"
    assert result.memories[0].category == "idea"
    assert result.memories[1].category == "household"


@pytest.mark.asyncio
async def test_extract_memories_no_api_key_returns_typed_status(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)
    monkeypatch.delenv("GOOGLE_API_KEY", raising=False)
    result = await postcall_memory._extract_memories("some transcript")
    assert result.memories == []
    assert result.status == "no_api_key"


@pytest.mark.asyncio
async def test_extract_memories_empty_transcript_returns_typed_status(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("GEMINI_API_KEY", "fake-key")
    result = await postcall_memory._extract_memories("")
    assert result.memories == []
    assert result.status == "no_transcript_text"
    result = await postcall_memory._extract_memories("    \n  \n")
    assert result.memories == []
    assert result.status == "no_transcript_text"


@pytest.mark.asyncio
async def test_extract_memories_malformed_json_returns_parse_failed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("GEMINI_API_KEY", "fake-key")

    mock_response = MagicMock()
    mock_response.text = "not valid json {{{"
    mock_models = MagicMock()
    mock_models.generate_content.return_value = mock_response
    mock_client = MagicMock()
    mock_client.models = mock_models

    with patch("sdk.postcall_memory.genai.Client", return_value=mock_client):
        result = await postcall_memory._extract_memories("transcript")
    assert result.memories == []
    assert result.status == "parse_failed"


@pytest.mark.asyncio
async def test_extract_memories_gemini_raises_transport_returns_transport_failed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("GEMINI_API_KEY", "fake-key")

    mock_models = MagicMock()
    mock_models.generate_content.side_effect = RuntimeError("network blew up")
    mock_client = MagicMock()
    mock_client.models = mock_models

    with patch("sdk.postcall_memory.genai.Client", return_value=mock_client):
        result = await postcall_memory._extract_memories("transcript")
    assert result.memories == []
    assert result.status == "transport_failed"


@pytest.mark.asyncio
async def test_extract_memories_gemini_401_returns_auth_failed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The 2026-05-15 voice-path silent-loss case: stale Gemini key returns 401.

    Pre-fix: this surfaced as `status=empty_extraction`, indistinguishable
    from a genuinely uneventful call. Three days of voice memories were
    lost before the operator noticed.
    """
    monkeypatch.setenv("GEMINI_API_KEY", "fake-key")

    mock_models = MagicMock()
    mock_models.generate_content.side_effect = RuntimeError(
        "401 UNAUTHENTICATED. Request had invalid authentication credentials."
    )
    mock_client = MagicMock()
    mock_client.models = mock_models

    with patch("sdk.postcall_memory.genai.Client", return_value=mock_client):
        result = await postcall_memory._extract_memories("transcript")
    assert result.memories == []
    assert result.status == "auth_failed"


@pytest.mark.asyncio
async def test_extract_memories_gemini_403_returns_auth_failed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("GEMINI_API_KEY", "fake-key")
    mock_models = MagicMock()
    mock_models.generate_content.side_effect = RuntimeError(
        "403 PERMISSION_DENIED. Quota exceeded."
    )
    mock_client = MagicMock()
    mock_client.models = mock_models
    with patch("sdk.postcall_memory.genai.Client", return_value=mock_client):
        result = await postcall_memory._extract_memories("transcript")
    assert result.status == "auth_failed"


@pytest.mark.asyncio
async def test_extract_memories_drops_invalid_entries(monkeypatch: pytest.MonkeyPatch) -> None:
    """Missing content gets dropped; valid neighbours survive."""
    monkeypatch.setenv("GEMINI_API_KEY", "fake-key")

    fake_response_text = json.dumps(
        {
            "memories": [
                {"content": ""},  # bad — empty content
                {"summary": "no content here"},  # bad — no content key
                {"content": "Real memory content", "category": "personal"},
                "not a dict",  # bad — wrong shape
            ]
        }
    )

    mock_response = MagicMock()
    mock_response.text = fake_response_text
    mock_models = MagicMock()
    mock_models.generate_content.return_value = mock_response
    mock_client = MagicMock()
    mock_client.models = mock_models

    with patch("sdk.postcall_memory.genai.Client", return_value=mock_client):
        result = await postcall_memory._extract_memories("transcript")

    assert result.status == "extracted"
    assert len(result.memories) == 1
    assert result.memories[0].content == "Real memory content"


@pytest.mark.asyncio
async def test_extract_memories_no_memories_key_returns_parse_failed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("GEMINI_API_KEY", "fake-key")

    mock_response = MagicMock()
    mock_response.text = json.dumps({"unrelated": "data"})
    mock_models = MagicMock()
    mock_models.generate_content.return_value = mock_response
    mock_client = MagicMock()
    mock_client.models = mock_models

    with patch("sdk.postcall_memory.genai.Client", return_value=mock_client):
        result = await postcall_memory._extract_memories("transcript")
    assert result.memories == []
    # `parse_failed` because the response is JSON but lacks the expected
    # `memories` array — same status as malformed JSON, both are
    # "Gemini didn't give us the shape we expected".
    assert result.status == "parse_failed"


@pytest.mark.asyncio
async def test_extract_memories_zero_valid_memories_returns_empty_extraction(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Distinct from `parse_failed`: Gemini gave a valid `memories` array
    but every entry was rejected by `_validate_memory`. Treated as
    `empty_extraction` because the conversation was reached, parsed, and
    Gemini's response was structurally fine — just nothing extractable.
    """
    monkeypatch.setenv("GEMINI_API_KEY", "fake-key")

    fake_response_text = json.dumps(
        {
            "memories": [
                {"content": ""},
                {"summary": "no content key here"},
                "not a dict",
            ]
        }
    )
    mock_response = MagicMock()
    mock_response.text = fake_response_text
    mock_models = MagicMock()
    mock_models.generate_content.return_value = mock_response
    mock_client = MagicMock()
    mock_client.models = mock_models

    with patch("sdk.postcall_memory.genai.Client", return_value=mock_client):
        result = await postcall_memory._extract_memories("transcript")
    assert result.memories == []
    assert result.status == "empty_extraction"


# --- _capture_one ----------------------------------------------------------


@pytest.mark.asyncio
async def test_capture_one_attaches_required_tags() -> None:
    captured_kwargs: dict[str, Any] = {}

    async def fake_capture(**kwargs: Any) -> dict[str, Any]:
        captured_kwargs.update(kwargs)
        return {"object_id": "test-id-123"}

    mock_client = MagicMock()
    mock_client.capture_memory = fake_capture

    memory = ExtractedMemory(
        content="Eric had a thought",
        summary="A thought",
        topics=["thinking", "morning"],
        category="reflection",
    )
    ok = await _capture_one(
        client=mock_client,
        namespace="nyla/voice/episodic",
        memory=memory,
        speaker_tag="nyla-voice",
        call_sid="test-sid",
    )
    assert ok is True
    assert captured_kwargs["namespace"] == "nyla/voice/episodic"
    assert captured_kwargs["content"] == "Eric had a thought"
    assert captured_kwargs["importance"] == 5
    tags = captured_kwargs["tags"]
    assert "thinking" in tags
    assert "morning" in tags
    assert "category:reflection" in tags
    assert "source:transcript" in tags
    assert "nyla-voice" in tags


@pytest.mark.asyncio
async def test_capture_one_returns_false_on_musubi_error() -> None:
    from sdk.musubi_v2_client import MusubiV2ServerError

    async def fake_capture(**_: Any) -> dict[str, Any]:
        raise MusubiV2ServerError("boom")

    mock_client = MagicMock()
    mock_client.capture_memory = fake_capture

    memory = ExtractedMemory(
        content="Eric had a thought",
        summary="A thought",
        topics=[],
        category="general",
    )
    ok = await _capture_one(
        client=mock_client,
        namespace="nyla/voice/episodic",
        memory=memory,
        speaker_tag="nyla-voice",
        call_sid="test-sid",
    )
    assert ok is False


# --- run_extraction (end-to-end with mocked transcript + Gemini + client) ---


@pytest.mark.asyncio
async def test_run_extraction_no_transcript_returns_zero(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv("LIVEKIT_VOICE_LOGS", str(tmp_path))
    # No transcript file at the expected path.
    captured = await run_extraction(
        call_sid="missing-sid",
        namespace="nyla/voice/episodic",
        speaker_tag="nyla-voice",
    )
    assert captured == 0


@pytest.mark.asyncio
async def test_run_extraction_full_loop(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """End-to-end: transcript on disk → mock Gemini → mock capture →
    correct count returned."""
    monkeypatch.setenv("LIVEKIT_VOICE_LOGS", str(tmp_path))
    monkeypatch.setenv("GEMINI_API_KEY", "fake-key")

    transcripts = tmp_path / "phone-transcripts"
    transcripts.mkdir(parents=True, exist_ok=True)
    (transcripts / "test-sid.txt").write_text(
        "[10:00:00] [USER] Hey Nyla\n"
        "[10:00:02] [ASSISTANT] Hi Eric\n"
        "[10:00:05] [USER] Let's talk about the embedding DB question\n"
    )

    fake_response_text = json.dumps(
        {
            "memories": [
                {
                    "content": "Eric wanted to talk about the embedding DB question",
                    "summary": "Embedding DB",
                    "topics": ["embedding-db"],
                    "category": "project",
                }
            ]
        }
    )
    mock_response = MagicMock()
    mock_response.text = fake_response_text
    mock_models = MagicMock()
    mock_models.generate_content.return_value = mock_response
    mock_genai_client = MagicMock()
    mock_genai_client.models = mock_models

    mock_musubi = MagicMock()
    mock_musubi.capture_memory = AsyncMock(return_value={"object_id": "test-obj"})

    with patch("sdk.postcall_memory.genai.Client", return_value=mock_genai_client):
        captured = await run_extraction(
            call_sid="test-sid",
            namespace="nyla/voice/episodic",
            speaker_tag="nyla-voice",
            client=mock_musubi,
        )

    assert captured == 1
    mock_musubi.capture_memory.assert_called_once()
    call_kwargs = mock_musubi.capture_memory.call_args.kwargs
    assert call_kwargs["namespace"] == "nyla/voice/episodic"
    assert "category:project" in call_kwargs["tags"]
    assert "source:transcript" in call_kwargs["tags"]


@pytest.mark.asyncio
async def test_run_extraction_auth_failure_logs_distinct_status(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """The 2026-05-15 silent-loss case, locked in as a regression test.

    Stale Gemini key → 401 from Gemini → `_extract_memories` returns
    `auth_failed` status → `run_extraction` propagates it to the
    completion log line as `status=auth_failed`, NOT `status=empty_extraction`.

    Pre-openclaw-livekit#29, this same scenario logged
    `status=empty_extraction`, indistinguishable from a genuinely
    uneventful call. If this assertion ever breaks, the silent-loss
    class is back.
    """
    import logging

    monkeypatch.setenv("LIVEKIT_VOICE_LOGS", str(tmp_path))
    monkeypatch.setenv("GEMINI_API_KEY", "fake-key")

    transcripts = tmp_path / "phone-transcripts"
    transcripts.mkdir(parents=True, exist_ok=True)
    (transcripts / "test-auth.txt").write_text(
        "[10:00:00] [USER] anything that should extract\n[10:00:02] [ASSISTANT] reply\n"
    )

    mock_models = MagicMock()
    mock_models.generate_content.side_effect = RuntimeError(
        "401 UNAUTHENTICATED. Request had invalid authentication credentials."
    )
    mock_genai_client = MagicMock()
    mock_genai_client.models = mock_models

    with (
        caplog.at_level(logging.INFO, logger="openclaw-livekit.agent"),
        patch("sdk.postcall_memory.genai.Client", return_value=mock_genai_client),
    ):
        captured = await run_extraction(
            call_sid="test-auth",
            namespace="nyla/voice/episodic",
            speaker_tag="nyla-voice",
            client=MagicMock(),  # never reached on auth failure
        )

    assert captured == 0
    # The decisive assertion: completion log says auth_failed, not
    # empty_extraction. This is the exact contract that hid the
    # 2026-05-15 voice silent-loss for three days before the fix.
    completion_logs = [
        r.message for r in caplog.records if "postcall_memory: completed" in r.message
    ]
    assert any("status=auth_failed" in m for m in completion_logs), (
        f"expected completion log with status=auth_failed; got: {completion_logs}"
    )


# --- _spawn_extraction_subprocess ------------------------------------------


def test_spawn_extraction_subprocess_invokes_popen_with_correct_args(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """The spawn helper should call subprocess.Popen with the expected
    `python -m sdk.postcall_memory` invocation, in detached mode."""
    monkeypatch.setenv("LIVEKIT_VOICE_LOGS", str(tmp_path))
    captured: dict[str, Any] = {}

    def fake_popen(args: list[str], **kwargs: Any) -> MagicMock:
        captured["args"] = args
        captured["kwargs"] = kwargs
        return MagicMock()

    with patch("sdk.postcall_memory.subprocess.Popen", side_effect=fake_popen):
        postcall_memory._spawn_extraction_subprocess(
            call_sid="test-sid",
            namespace="nyla/voice/episodic",
            speaker_tag="nyla-voice",
        )

    args = captured["args"]
    # python -m sdk.postcall_memory --call-sid X --namespace Y --speaker-tag Z
    assert args[1:3] == ["-m", "sdk.postcall_memory"]
    assert "--call-sid" in args and "test-sid" in args
    assert "--namespace" in args and "nyla/voice/episodic" in args
    assert "--speaker-tag" in args and "nyla-voice" in args

    # Detached spawn
    assert captured["kwargs"]["start_new_session"] is True
    assert captured["kwargs"]["close_fds"] is True


def test_spawn_extraction_subprocess_omits_speaker_tag_when_none(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv("LIVEKIT_VOICE_LOGS", str(tmp_path))
    captured: dict[str, Any] = {}

    def fake_popen(args: list[str], **kwargs: Any) -> MagicMock:
        captured["args"] = args
        return MagicMock()

    with patch("sdk.postcall_memory.subprocess.Popen", side_effect=fake_popen):
        postcall_memory._spawn_extraction_subprocess(
            call_sid="test-sid",
            namespace="nyla/voice/episodic",
            speaker_tag=None,
        )

    args = captured["args"]
    assert "--speaker-tag" not in args


def test_spawn_extraction_subprocess_swallows_popen_failure(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A failure to spawn (e.g. permission, missing binary) should log
    but not raise — Path B is best-effort and can't break the close
    handler that calls it."""
    monkeypatch.setenv("LIVEKIT_VOICE_LOGS", str(tmp_path))

    with patch(
        "sdk.postcall_memory.subprocess.Popen",
        side_effect=PermissionError("denied"),
    ):
        # Should NOT raise — that would propagate into the session.on('close')
        # handler and be silent on the user side.
        postcall_memory._spawn_extraction_subprocess(
            call_sid="test-sid",
            namespace="nyla/voice/episodic",
            speaker_tag="nyla-voice",
        )
