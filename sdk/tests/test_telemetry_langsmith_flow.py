from __future__ import annotations

from contextlib import contextmanager
from types import SimpleNamespace
from typing import Any

from sdk import telemetry


class FakeSpan:
    def __init__(self) -> None:
        self.attributes: dict[str, Any] = {}

    def set_attribute(self, key: str, value: Any) -> None:
        self.attributes[key] = value


class FakeTracer:
    def __init__(self) -> None:
        self.started: list[tuple[str, FakeSpan]] = []

    @contextmanager
    def start_as_current_span(self, name: str):
        span = FakeSpan()
        self.started.append((name, span))
        yield span


class LLMModelUsage:
    provider = "google"
    model = "gemini"
    input_tokens = 10
    input_cached_tokens = 2
    input_audio_tokens = 7
    input_text_tokens = 3
    output_tokens = 4
    output_audio_tokens = 1
    output_text_tokens = 3
    session_duration = 12.5


def test_conversation_span_renders_user_turn(monkeypatch) -> None:
    tracer = FakeTracer()
    monkeypatch.setattr(telemetry, "_tracer", tracer)

    telemetry.emit_conversation_span(
        call_sid="call-1",
        agent_name="phone-nyla",
        role="user",
        text="hello Nyla",
        metrics={"transcription_delay": 0.25},
    )

    name, span = tracer.started[0]
    assert name == "user_message"
    assert span.attributes["langsmith.span.tags"] == "conversation,role:user"
    assert span.attributes["langsmith.metadata.call_sid"] == "call-1"
    assert (
        span.attributes["input.value"]
        == '{"messages": [{"role": "user", "content": "hello Nyla"}]}'
    )
    assert span.attributes["gen_ai.prompt.0.content"] == "hello Nyla"
    assert span.attributes["langsmith.metadata.transcription_delay"] == 0.25
    assert span.attributes["langsmith.metadata.transcription_delay_ms"] == 250.0


def test_tool_span_renders_as_langsmith_tool(monkeypatch) -> None:
    tracer = FakeTracer()
    monkeypatch.setattr(telemetry, "_tracer", tracer)

    call = SimpleNamespace(name="musubi_search", arguments='{"query":"prank"}', call_id="tool-1")
    output = SimpleNamespace(output="Found memory row", is_error=False)

    telemetry._emit_tool_span(
        call_sid="call-1",
        agent_name="phone-nyla",
        call=call,
        output=output,
    )

    name, span = tracer.started[0]
    assert name == "musubi_search"
    assert span.attributes["langsmith.span.kind"] == "tool"
    assert span.attributes["gen_ai.tool.name"] == "musubi_search"
    assert span.attributes["gen_ai.tool.call.id"] == "tool-1"
    assert span.attributes["langsmith.metadata.tool_call_id"] == "tool-1"
    assert span.attributes["langsmith.metadata.tool_arguments"] == '{"query":"prank"}'
    assert span.attributes["langsmith.metadata.tool_result"] == "Found memory row"
    assert span.attributes["gen_ai.prompt.0.content"] == 'call musubi_search({"query":"prank"})'
    assert span.attributes["gen_ai.completion.0.role"] == "tool"
    assert span.attributes["gen_ai.completion.0.content"] == "Found memory row"


def test_usage_attrs_promote_totals() -> None:
    ev = SimpleNamespace(usage=SimpleNamespace(model_usage=[LLMModelUsage()]))

    attrs = telemetry._usage_attrs_from_event(ev)

    assert attrs["llmmodelusage.gemini.input_tokens"] == 10
    assert attrs["llmmodelusage.gemini.input_cached_tokens"] == 2
    assert attrs["llmmodelusage.gemini.input_audio_tokens"] == 7
    assert attrs["llmmodelusage.gemini.output_audio_tokens"] == 1
    assert attrs["llmmodelusage.gemini.session_duration"] == 12.5
    assert attrs["llmmodelusage.gemini.session_duration_ms"] == 12500.0
    assert attrs["llmmodelusage.gemini.output_tokens"] == 4
    assert attrs["usage.input_tokens"] == 10
    assert attrs["usage.input_cached_tokens"] == 2
    assert attrs["usage.input_audio_tokens"] == 7
    assert attrs["usage.output_tokens"] == 4
    assert attrs["usage.total_tokens"] == 14
