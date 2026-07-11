"""Tests for the post-call manifest (``sdk.postcall``).

``wire_postcall_review`` is registered on every agent's session, yet had zero
coverage before this file. The manifest is an append-only audit log of every
closed call; there is no automated reviewer (the CLI gateway that spawned Rin
is retired), so entries land as ``no_reviewer`` or ``skipped_no_transcript``.
These tests pin that behaviour and the ``$LIVEKIT_VOICE_LOGS``-unset no-op.
"""

from __future__ import annotations

import json
from collections.abc import Callable
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast

import pytest

from sdk import postcall


class _FakeSession:
    """Duck-typed stand-in for ``AgentSession`` — captures the ``close``
    handler ``wire_postcall_review`` registers so tests can fire it."""

    def __init__(self) -> None:
        self.handlers: dict[str, Callable[[Any], None]] = {}

    def on(self, event: str) -> Callable[[Callable[[Any], None]], Callable[[Any], None]]:
        def _register(fn: Callable[[Any], None]) -> Callable[[Any], None]:
            self.handlers[event] = fn
            return fn

        return _register


def _close_event(reason: str = "user_hangup", error: object | None = None) -> SimpleNamespace:
    return SimpleNamespace(reason=reason, error=error)


def _read_manifest(logs: Path) -> list[dict[str, Any]]:
    path = logs / "call-manifest.jsonl"
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text().splitlines() if line.strip()]


def _make_transcript(logs: Path, call_sid: str) -> None:
    tdir = logs / "phone-transcripts"
    tdir.mkdir(parents=True, exist_ok=True)
    (tdir / f"{call_sid}.txt").write_text("USER: hi\nAGENT: hello\n")


@pytest.fixture()
def voice_logs(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setenv("LIVEKIT_VOICE_LOGS", str(tmp_path))
    return tmp_path


def _fire_close(session: _FakeSession, event: object) -> None:
    session.handlers["close"](event)


def _wire(session: _FakeSession, **kwargs: Any) -> None:
    """Call the real wiring with the duck-typed fake (cast past the
    ``AgentSession`` annotation — the function only uses ``.on``)."""
    postcall.wire_postcall_review(cast(Any, session), **kwargs)


# --- wiring guard ---------------------------------------------------------


def test_no_call_sid_registers_no_handler(voice_logs: Path) -> None:
    """Without a call_sid there is nothing to key a manifest entry on, so the
    handler must not be registered at all."""
    session = _FakeSession()
    _wire(session, call_sid=None, agent_name="aoi")
    assert "close" not in session.handlers


def test_wire_registers_close_handler(voice_logs: Path) -> None:
    session = _FakeSession()
    _wire(session, call_sid="SCL_1", agent_name="aoi")
    assert "close" in session.handlers


# --- manifest branches ----------------------------------------------------


def test_closed_call_with_transcript_records_no_reviewer(voice_logs: Path) -> None:
    """The load-bearing branch: a normal closed call with a transcript is
    recorded as ``no_reviewer`` — the status a future sweep keys on."""
    _make_transcript(voice_logs, "SCL_2")
    session = _FakeSession()
    _wire(session, call_sid="SCL_2", agent_name="aoi")

    _fire_close(session, _close_event(reason="user_hangup"))

    entries = _read_manifest(voice_logs)
    assert len(entries) == 1
    entry = entries[0]
    assert entry["call_sid"] == "SCL_2"
    assert entry["agent"] == "aoi"
    assert entry["review_status"] == "no_reviewer"
    assert entry["has_transcript"] is True
    assert entry["has_error"] is False
    assert entry["close_reason"] == "user_hangup"


def test_closed_call_without_transcript_records_skipped(voice_logs: Path) -> None:
    session = _FakeSession()
    _wire(session, call_sid="SCL_3", agent_name="nyla")

    _fire_close(session, _close_event(reason="no_answer"))

    entries = _read_manifest(voice_logs)
    assert len(entries) == 1
    assert entries[0]["review_status"] == "skipped_no_transcript"
    assert entries[0]["has_transcript"] is False


def test_error_on_close_is_captured(voice_logs: Path) -> None:
    _make_transcript(voice_logs, "SCL_4")
    session = _FakeSession()
    _wire(session, call_sid="SCL_4", agent_name="yua")

    _fire_close(session, _close_event(reason="error", error=RuntimeError("media plane died")))

    entry = _read_manifest(voice_logs)[0]
    assert entry["has_error"] is True
    assert "media plane died" in entry["error_detail"]


def test_multiple_calls_append(voice_logs: Path) -> None:
    """The manifest is append-only — two closes yield two lines, not a rewrite."""
    for sid in ("SCL_5", "SCL_6"):
        _make_transcript(voice_logs, sid)
        session = _FakeSession()
        _wire(session, call_sid=sid, agent_name="aoi")
        _fire_close(session, _close_event())

    entries = _read_manifest(voice_logs)
    assert [e["call_sid"] for e in entries] == ["SCL_5", "SCL_6"]


def test_agent_name_is_required_not_defaulted_to_unknown() -> None:
    """This test used to be `test_default_agent_name_is_unknown` — it ASSERTED THE BUG.

    `agent_name` defaulted to "unknown". All 12 call sites pass it, so the default never
    fired: it existed only to SWALLOW a future wiring mistake. And what it would swallow is an
    identity error — a call review filed under "unknown", silently, forever, while every
    per-agent dashboard panel and alert selector (`voice-.*`) matched nothing and looked fine.

    A default in the identity path is not a convenience. It is a misattribution waiting for the
    first person who forgets an argument. Same lesson as ENV AGENT=aoi, the default persona,
    and voice="Leda".

    The old test locked the default in place. This one refuses it.
    """
    import inspect

    from sdk.postcall import wire_postcall_review

    param = inspect.signature(wire_postcall_review).parameters["agent_name"]
    assert param.default is inspect.Parameter.empty, (
        f"wire_postcall_review(agent_name=...) defaults to {param.default!r}. A call review "
        f"attributed to nobody is worse than a crash — the crash you fix, the misattribution "
        f"you never notice."
    )


# --- $LIVEKIT_VOICE_LOGS unset → no-op ------------------------------------


def test_no_voice_logs_env_is_a_noop(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """With the env var unset, post-call review must silently no-op rather than
    crash the close handler on a live call."""
    monkeypatch.delenv("LIVEKIT_VOICE_LOGS", raising=False)
    session = _FakeSession()
    _wire(session, call_sid="SCL_8", agent_name="aoi")
    # Handler still registers; firing it must not raise and must write nothing.
    _fire_close(session, _close_event())
    assert not (tmp_path / "call-manifest.jsonl").exists()
