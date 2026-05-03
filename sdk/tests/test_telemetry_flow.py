"""Tests for :class:`TelemetryCollector` — the per-call JSON writer.

The JSON file at ``$LIVEKIT_VOICE_LOGS/call-telemetry/{call_sid}.json`` is
the primary input to ``postcall.py``. These tests pin its shape: turn
metrics, realtime metrics, tool calls, errors, summary stats.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast

from livekit.agents import AgentSession

from sdk import telemetry


class _FakeRealtimeMetrics:
    """Same shape as ``livekit.agents.metrics.RealtimeModelMetrics``.

    ``record_realtime_metrics`` dispatches by class name; matching the
    name is the only contract that matters here.
    """

    def __init__(self, **fields: Any) -> None:
        for key, value in fields.items():
            setattr(self, key, value)


_FakeRealtimeMetrics.__name__ = "RealtimeModelMetrics"


def _setup_logs_dir(tmp_path: Path, monkeypatch) -> Path:
    monkeypatch.setenv("LIVEKIT_VOICE_LOGS", str(tmp_path))
    return tmp_path / "call-telemetry"


def test_record_turn_captures_chained_pipeline_metrics() -> None:
    c = telemetry.TelemetryCollector("call-1", "phone-party")

    c.record_turn(
        {
            "e2e_latency": 1.234,
            "llm_node_ttft": 0.456,
            "tts_node_ttfb": 0.123,
            "transcription_delay": 0.78,
            "end_of_turn_delay": 0.05,
            "on_user_turn_completed_delay": 0.02,
            "extra_unknown_field": 999,
        },
        role="assistant",
        text="hello back",
    )

    assert len(c.turns) == 1
    entry = c.turns[0]
    assert entry["turn_index"] == 0
    assert entry["role"] == "assistant"
    assert entry["text_preview"] == "hello back"
    assert entry["e2e_latency"] == 1.234
    assert entry["llm_node_ttft"] == 0.456
    assert entry["tts_node_ttfb"] == 0.123
    assert entry["transcription_delay"] == 0.78
    assert "extra_unknown_field" not in entry


def test_record_realtime_metrics_captures_token_breakdown() -> None:
    c = telemetry.TelemetryCollector("call-1", "phone-nyla")
    ev = _FakeRealtimeMetrics(
        request_id="req-7",
        ttft=0.42,
        duration=2.5,
        input_tokens=120,
        output_tokens=60,
        total_tokens=180,
        tokens_per_second=24.0,
        input_token_details="audio=80,text=40",
    )

    c.record_realtime_metrics(ev)

    assert len(c.realtime_metrics) == 1
    entry = c.realtime_metrics[0]
    assert entry["request_id"] == "req-7"
    assert entry["ttft"] == 0.42
    assert entry["duration"] == 2.5
    assert entry["input_tokens"] == 120
    assert entry["output_tokens"] == 60
    assert entry["total_tokens"] == 180
    assert entry["tokens_per_second"] == 24.0
    assert entry["input_token_details"] == "audio=80,text=40"


def test_record_tool_execution_captures_calls_and_errors() -> None:
    c = telemetry.TelemetryCollector("call-1", "phone-nyla")
    ok_call = SimpleNamespace(name="musubi_search", arguments='{"q":"x"}', call_id="t1")
    ok_output = SimpleNamespace(output="found", is_error=False)
    bad_call = SimpleNamespace(name="musubi_get", arguments="{}", call_id="t2")
    bad_output = SimpleNamespace(output="not yet implemented", is_error=True)
    ev = SimpleNamespace(
        function_calls=[ok_call, bad_call],
        function_call_outputs=[ok_output, bad_output],
    )

    c.record_tool_execution(ev)

    assert len(c.tool_calls) == 2
    assert c.tool_calls[0]["name"] == "musubi_search"
    assert c.tool_calls[0]["success"] is True
    assert c.tool_calls[0]["output"] == "found"
    assert c.tool_calls[1]["name"] == "musubi_get"
    assert c.tool_calls[1]["success"] is False
    assert c.tool_calls[1]["is_error"] is True


def test_record_overlap_distinguishes_interruption_from_backchannel() -> None:
    c = telemetry.TelemetryCollector("call-1", "phone-nyla")
    interruption = SimpleNamespace(
        is_interruption=True,
        probability=0.9,
        detection_delay=0.04,
        prediction_duration=None,
        total_duration=0.2,
    )
    backchannel = SimpleNamespace(
        is_interruption=False,
        probability=0.3,
        detection_delay=0.06,
        prediction_duration=None,
        total_duration=0.05,
    )

    c.record_overlap(interruption)
    c.record_overlap(backchannel)
    summary = c.build_summary()

    assert summary["interruptions"] == 1
    assert summary["backchannels"] == 1
    assert summary["overlapping_speech_events"] == 2


def test_summary_uses_realtime_ttft_when_chained_metrics_absent() -> None:
    """Realtime models (Gemini realtime) emit TTFT via ``metrics_collected``,
    not via per-turn ``ChatMessage.metrics``. The summary exposes that as
    TTFT, not e2e latency.
    """
    c = telemetry.TelemetryCollector("call-1", "phone-nyla")
    c.record_realtime_metrics(_FakeRealtimeMetrics(request_id="r1", ttft=0.4, duration=1.0))
    c.record_realtime_metrics(_FakeRealtimeMetrics(request_id="r2", ttft=0.6, duration=1.5))
    # ttft = -1 means "no audio token sent" — must be excluded.
    c.record_realtime_metrics(_FakeRealtimeMetrics(request_id="r3", ttft=-1, duration=0))

    summary = c.build_summary()

    assert summary["e2e_latency"]["count"] == 0
    assert summary["llm_ttft"]["count"] == 2
    assert summary["llm_ttft"]["min"] == 0.4
    assert summary["llm_ttft"]["max"] == 0.6
    assert summary["realtime_ttft"]["count"] == 2
    assert summary["realtime_ttft"]["avg"] == 0.5


def test_flush_writes_json_with_complete_shape(tmp_path, monkeypatch) -> None:
    expected_dir = _setup_logs_dir(tmp_path, monkeypatch)
    c = telemetry.TelemetryCollector("call-1", "phone-nyla")
    c.record_turn({"e2e_latency": 1.0}, role="assistant", text="hey")
    c.record_realtime_metrics(_FakeRealtimeMetrics(request_id="r", ttft=0.5, duration=1.0))
    c.record_user_state("listening", "speaking")
    c.record_agent_state("idle", "thinking")
    c.record_overlap(SimpleNamespace(is_interruption=True, probability=0.8))
    c.record_tool_execution(
        SimpleNamespace(
            function_calls=[SimpleNamespace(name="t", arguments="{}", call_id="x")],
            function_call_outputs=[SimpleNamespace(output="ok", is_error=False)],
        )
    )
    c.record_close(SimpleNamespace(reason="user_hangup"))

    path = c.flush()

    assert path is not None
    assert path == expected_dir / "call-1.json"
    doc = json.loads(path.read_text())
    assert doc["call_sid"] == "call-1"
    assert doc["agent"] == "phone-nyla"
    assert doc["close_reason"] == "user_hangup"
    assert doc["latency_source"] == "chained"
    assert doc["summary"]["total_turns"] == 1
    assert doc["summary"]["e2e_latency"]["count"] == 1
    assert doc["summary"]["realtime_ttft"]["count"] == 1
    assert doc["summary"]["interruptions"] == 1
    assert doc["summary"]["tool_calls_total"] == 1
    assert len(doc["turns"]) == 1
    assert len(doc["realtime_metrics"]) == 1
    assert len(doc["tool_calls"]) == 1
    assert len(doc["user_states"]) == 1
    assert len(doc["agent_states"]) == 1


def test_flush_is_noop_when_logs_env_missing(tmp_path, monkeypatch) -> None:
    """Without ``LIVEKIT_VOICE_LOGS`` set, capture is a no-op — agents
    that don't opt into telemetry capture must not crash on close."""
    monkeypatch.delenv("LIVEKIT_VOICE_LOGS", raising=False)
    c = telemetry.TelemetryCollector("call-1", "phone-nyla")

    assert c.flush() is None


def test_wire_telemetry_capture_returns_none_without_call_sid() -> None:
    """Synthetic ``sim-*`` rooms have no SIP call_id; we don't capture."""
    session = cast(AgentSession, SimpleNamespace(on=lambda *_a, **_kw: lambda f: f))

    assert telemetry.wire_telemetry_capture(session, call_sid=None, agent_name="x") is None


def test_wire_telemetry_capture_subscribes_all_session_events(monkeypatch) -> None:
    """Pin the listener contract: every session event we capture must
    register an ``on(...)`` handler. If ``AgentSession`` adds or renames
    one, this test must be updated alongside ``TelemetryCollector``."""
    registered: list[str] = []

    def on(event_name: str):
        def decorator(fn):
            registered.append(event_name)
            return fn

        return decorator

    session = cast(AgentSession, SimpleNamespace(on=on))

    collector = telemetry.wire_telemetry_capture(session, call_sid="c1", agent_name="phone-nyla")

    assert collector is not None
    assert set(registered) == {
        "conversation_item_added",
        "user_state_changed",
        "agent_state_changed",
        "overlapping_speech",
        "agent_false_interruption",
        "function_tools_executed",
        "session_usage_updated",
        "metrics_collected",
        "error",
        "close",
    }


def teardown_module(module) -> None:  # noqa: ARG001
    os.environ.pop("LIVEKIT_VOICE_LOGS", None)
