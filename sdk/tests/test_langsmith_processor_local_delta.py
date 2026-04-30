"""Tests for the LOCAL DELTA branches we added to the vendored
``LangSmithSpanProcessor`` for LiveKit-specific span types.

The vendored processor (``sdk/src/sdk/langsmith_processor.py``) is
verbatim-from-upstream EXCEPT for two branches we added: one for
``function_tool`` spans (LiveKit emits one per tool invocation) and
one for ``user_turn`` spans (LiveKit emits one per user-spoken turn
with the STT transcript on ``lk.user_transcript``). These tests pin
the contract of those branches so an upstream re-sync that overwrites
them surfaces immediately as a test failure.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import MagicMock

from sdk.langsmith_processor import LangSmithSpanProcessor


def _make_span(name: str, attributes: dict[str, Any]) -> MagicMock:
    """Build a minimal ReadableSpan-like mock that the processor's
    ``on_end`` can read + write attributes against.

    Production ReadableSpans are immutable from outside but the upstream
    processor reaches in via ``span._attributes[...] = ...`` — we mirror
    that contract here with a real dict so assertions can read it back.
    """
    span = MagicMock()
    span.name = name
    span.context.trace_id = 0xDEADBEEFCAFE
    span.context.span_id = 0xABCD1234
    span.parent = None
    span._attributes = dict(attributes)  # type: ignore[attr-defined]
    span.attributes = span._attributes  # processor reads via .attributes too
    return span


def _make_processor() -> LangSmithSpanProcessor:
    """Processor with a no-op downstream so we can inspect the span
    state without an HTTP export firing."""
    downstream = MagicMock()
    return LangSmithSpanProcessor(downstream_processor=downstream)


# ---------------------------------------------------------------------------
# function_tool branch
# ---------------------------------------------------------------------------


def test_function_tool_span_renders_as_tool_kind() -> None:
    """LangSmith UI groups spans by ``langsmith.span.kind``. Tool calls
    must surface as ``"tool"`` so the call/result pair renders as a
    distinct exchange instead of a content-less generic chain span."""
    processor = _make_processor()
    span = _make_span(
        "function_tool",
        {
            "lk.function_tool.id": "call_123",
            "lk.function_tool.name": "musubi_search",
            "lk.function_tool.arguments": '{"query": "the prank"}',
            "lk.function_tool.output": "Found cocoa-pods row in nyla/openclaw/episodic",
            "lk.function_tool.is_error": False,
        },
    )

    processor.on_end(span)

    assert span._attributes["langsmith.span.kind"] == "tool"


def test_function_tool_span_extracts_name_and_args_into_prompt() -> None:
    """The tool name + arguments must land in the prompt so the LangSmith
    span sidebar shows ``call musubi_search({"query": "..."})`` — that's
    the affordance Eric needs to see WHICH tool fired with WHAT args."""
    processor = _make_processor()
    span = _make_span(
        "function_tool",
        {
            "lk.function_tool.name": "musubi_search",
            "lk.function_tool.arguments": '{"query": "prank"}',
            "lk.function_tool.output": "ok",
        },
    )

    processor.on_end(span)

    prompt_content = span._attributes["gen_ai.prompt.0.content"]
    assert "musubi_search" in prompt_content
    assert '"query": "prank"' in prompt_content


def test_function_tool_span_extracts_output_into_completion() -> None:
    """The tool result must land in the completion. Without this the
    LangSmith span shows the call but not what came back — half a debug
    surface."""
    processor = _make_processor()
    span = _make_span(
        "function_tool",
        {
            "lk.function_tool.name": "musubi_remember",
            "lk.function_tool.arguments": '{"content": "X"}',
            "lk.function_tool.output": "Saved as object_id=abc123",
        },
    )

    processor.on_end(span)

    completion_content = span._attributes["gen_ai.completion.0.content"]
    assert "abc123" in completion_content
    assert span._attributes["gen_ai.completion.0.role"] == "tool"


def test_function_tool_error_path_marks_completion() -> None:
    """When the tool errored, the completion must say so visibly so the
    debugging view in LangSmith doesn't conflate an error with a real
    answer. Especially important for the deferred-stub ``musubi_get``
    which "fails" by design until the SDK extension lands."""
    processor = _make_processor()
    span = _make_span(
        "function_tool",
        {
            "lk.function_tool.name": "musubi_get",
            "lk.function_tool.arguments": "{}",
            "lk.function_tool.output": "musubi_get is not yet available",
            "lk.function_tool.is_error": True,
        },
    )

    processor.on_end(span)

    completion = span._attributes["gen_ai.completion.0.content"]
    assert completion.startswith("[error]")
    assert "not yet available" in completion


# ---------------------------------------------------------------------------
# user_turn branch
# ---------------------------------------------------------------------------


def test_user_turn_span_extracts_transcript() -> None:
    """LiveKit emits ``user_turn`` spans with the STT transcript on
    ``lk.user_transcript``. Without our branch the LangSmith UI shows
    "Turn 0" / "Turn 1" with no content — that's the gap Eric flagged
    after the first live trace."""
    processor = _make_processor()
    span = _make_span(
        "user_turn",
        {
            "lk.user_transcript": "Hey Aoi, do you remember the prank we discussed?",
            "lk.transcript_confidence": 0.94,
        },
    )

    processor.on_end(span)

    assert (
        span._attributes["gen_ai.prompt.0.content"]
        == "Hey Aoi, do you remember the prank we discussed?"
    )
    assert span._attributes["gen_ai.prompt.0.role"] == "user"


def test_user_turn_span_surfaces_confidence_as_metadata() -> None:
    """Transcript confidence is useful sidebar context for "did Whisper
    really hear that?". Surface it as ``langsmith.metadata.*`` so the
    UI puts it in the span properties panel."""
    processor = _make_processor()
    span = _make_span(
        "user_turn",
        {
            "lk.user_transcript": "test",
            "lk.transcript_confidence": 0.87,
        },
    )

    processor.on_end(span)

    assert span._attributes["langsmith.metadata.transcript_confidence"] == "0.87"


def test_user_turn_with_empty_transcript_does_not_set_prompt() -> None:
    """An empty transcript shouldn't pollute the span with empty prompt
    attributes — that just adds noise to the LangSmith UI. Skip the
    enrichment when there's nothing to surface."""
    processor = _make_processor()
    span = _make_span(
        "user_turn",
        {"lk.user_transcript": ""},
    )

    processor.on_end(span)

    assert "gen_ai.prompt.0.content" not in span._attributes


# ---------------------------------------------------------------------------
# Re-sync regression guards — pin upstream span types we DO still handle
# ---------------------------------------------------------------------------


def test_llm_node_span_still_routes_to_llm_branch() -> None:
    """If an upstream re-sync drops the LLM branch we'd lose the prompt
    chain rendering. Guard against that so the next sync surfaces it."""
    processor = _make_processor()
    span = _make_span(
        "llm_node",
        {
            "lk.chat_ctx": '{"items": [{"type": "message", "role": "user", "content": "hello"}]}',
            "lk.response.text": "hi there",
        },
    )

    processor.on_end(span)

    assert span._attributes["langsmith.span.kind"] == "llm"
