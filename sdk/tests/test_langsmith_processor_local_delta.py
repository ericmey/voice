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


# ---------------------------------------------------------------------------
# LOCAL DELTA #3: universal metadata enrichment
#
# The whole point of `_enrich_universal_metadata` is making "where is the
# dead air" answerable in LangSmith. Each test below pins a metadata
# field that, if dropped, would re-blind us to a specific stage of
# per-turn latency. Treat failures here as "we lost a diagnostic axis."
# ---------------------------------------------------------------------------


def test_universal_metadata_propagates_ttft_to_sidebar() -> None:
    """`lk.response.ttft` is THE LLM-side TTFT metric. Without it
    surfaced as `langsmith.metadata.ttft_ms` the sidebar can't show
    per-turn LLM latency at a glance — exactly the diagnostic that
    answers Eric's `silence_duration_ms` vs `LLM-slow` question."""
    processor = _make_processor()
    span = _make_span(
        "llm_node",
        {"lk.response.ttft": 0.4503, "lk.chat_ctx": "{}"},
    )

    processor.on_end(span)

    assert span._attributes["langsmith.metadata.ttft_ms"] == "0.4503"


def test_universal_metadata_propagates_endpointing_delay() -> None:
    """`lk.eou.endpointing_delay` is the VAD silence wait — the metric
    most likely to expose the `silence_duration_ms=1000` blocker. Has
    to surface or we can't see it in LangSmith."""
    processor = _make_processor()
    span = _make_span(
        "eou_detection",
        {"lk.eou.endpointing_delay": 1.0, "lk.eou.probability": 0.92},
    )

    processor.on_end(span)

    assert span._attributes["langsmith.metadata.endpointing_delay_ms"] == "1.0"
    assert span._attributes["langsmith.metadata.eou_probability"] == "0.92"


def test_universal_metadata_propagates_e2e_latency() -> None:
    """Per-turn end-to-end latency. The single most useful number on
    a turn span — what the user actually felt as "the gap"."""
    processor = _make_processor()
    span = _make_span(
        "user_turn",
        {"lk.e2e_latency": 2.86, "lk.user_transcript": "hi"},
    )

    processor.on_end(span)

    assert span._attributes["langsmith.metadata.e2e_latency_ms"] == "2.86"


def test_universal_metadata_propagates_call_identity() -> None:
    """Agent name, room name, and participant identity (the phone number
    on SIP calls) must surface so traces are filterable in LangSmith.
    Without them you can't say "show me every nyla call from +1317...."."""
    processor = _make_processor()
    span = _make_span(
        "agent_session",
        {
            "lk.agent_name": "phone-nyla",
            "lk.room_name": "phone_+13179957066_abc",
            "lk.participant_identity": "+13179957066",
            "lk.job_id": "AJ_xyz123",
        },
    )

    processor.on_end(span)

    assert span._attributes["langsmith.metadata.agent"] == "phone-nyla"
    assert span._attributes["langsmith.metadata.room"] == "phone_+13179957066_abc"
    assert span._attributes["langsmith.metadata.user_id"] == "+13179957066"


def test_session_id_links_to_job_id() -> None:
    """`langsmith.trace.session_id` makes the LangSmith Threads view
    group all turns from one call. Mapping from `lk.job_id` is the
    only sane source — it's the per-call identifier LiveKit uses."""
    processor = _make_processor()
    span = _make_span(
        "agent_session",
        {"lk.job_id": "AJ_xyz123", "lk.agent_name": "phone-nyla"},
    )

    processor.on_end(span)

    assert span._attributes["langsmith.trace.session_id"] == "AJ_xyz123"


def test_universal_metadata_tags_realtime_pipeline() -> None:
    """Pipeline shape (realtime vs chained) is a high-value filter for
    diagnosis. Set it as a tag so operators can do `pipeline:realtime`
    queries in LangSmith and see only native-audio traces."""
    processor = _make_processor()
    span = _make_span(
        "llm_node",
        {
            "lk.agent_name": "phone-nyla",
            "lk.realtime_model_metrics": '{"ttft": 0.45, "audio_duration": 11.4}',
            "lk.chat_ctx": "{}",
        },
    )

    processor.on_end(span)

    tags = span._attributes["langsmith.span.tags"]
    assert "agent:phone-nyla" in tags
    assert "pipeline:realtime" in tags


def test_universal_metadata_tags_chained_pipeline() -> None:
    """Chained pipeline (Whisper + text LLM + TTS) must be tagged
    differently from realtime so traces from Party don't pollute
    realtime-only diagnostic queries."""
    processor = _make_processor()
    span = _make_span(
        "tts_node",
        {
            "lk.agent_name": "phone-party",
            "lk.tts_metrics": '{"ttfb": 0.15, "audio_duration": 2.3}',
        },
    )

    processor.on_end(span)

    tags = span._attributes["langsmith.span.tags"]
    assert "pipeline:chained" in tags


def test_realtime_metrics_json_blob_flattens_to_metadata() -> None:
    """LiveKit emits a JSON blob with rich realtime-model metrics
    (ttft, audio token counts, durations). Parse and flatten so each
    field becomes its own searchable metadata key — without this,
    operators can't query "show me turns where audio_duration > 10s"."""
    processor = _make_processor()
    span = _make_span(
        "realtime_metrics",
        {
            "lk.realtime_model_metrics": (
                '{"ttft": 0.45, "input_tokens": 6222, "output_tokens": 295,'
                ' "input_audio_tokens": 1200}'
            ),
        },
    )

    processor.on_end(span)

    assert span._attributes["langsmith.metadata.realtime_model.ttft"] == "0.45"
    assert span._attributes["langsmith.metadata.realtime_model.output_tokens"] == "295"
    assert span._attributes["langsmith.metadata.realtime_model.input_audio_tokens"] == "1200"


def test_function_tool_emits_otel_semantic_convention_attrs() -> None:
    """OTel's `gen_ai.tool.name` and `gen_ai.tool.call.id` are the
    standardized names LangSmith reads for tool grouping. Surface them
    alongside our `lk.*`-derived attributes so the UI's tool-call view
    works regardless of which schema LangSmith picks first."""
    processor = _make_processor()
    span = _make_span(
        "function_tool",
        {
            "lk.function_tool.id": "call_xyz",
            "lk.function_tool.name": "musubi_search",
            "lk.function_tool.arguments": '{"query": "x"}',
            "lk.function_tool.output": "ok",
        },
    )

    processor.on_end(span)

    assert span._attributes["gen_ai.tool.name"] == "musubi_search"
    assert span._attributes["gen_ai.tool.call.id"] == "call_xyz"
    assert span._attributes["gen_ai.operation.name"] == "execute_tool"


def test_function_tool_tags_with_tool_name() -> None:
    """`tool:musubi_search` as a tag lets operators filter LangSmith
    traces by which tool fired — single most useful slice for tracking
    a tool's behaviour over many calls."""
    processor = _make_processor()
    span = _make_span(
        "function_tool",
        {
            "lk.function_tool.name": "musubi_remember",
            "lk.function_tool.arguments": "{}",
            "lk.function_tool.output": "ok",
        },
    )

    processor.on_end(span)

    assert "tool:musubi_remember" in span._attributes["langsmith.span.tags"]


def test_function_tool_error_adds_error_tag() -> None:
    """When a tool errors, it should be discoverable via `error` tag —
    operators want to find every tool failure across all calls in one
    LangSmith query, not by clicking through traces."""
    processor = _make_processor()
    span = _make_span(
        "function_tool",
        {
            "lk.function_tool.name": "musubi_get",
            "lk.function_tool.arguments": "{}",
            "lk.function_tool.output": "not yet wired",
            "lk.function_tool.is_error": True,
        },
    )

    processor.on_end(span)

    assert "error" in span._attributes["langsmith.span.tags"]


def test_universal_metadata_skips_missing_attrs_silently() -> None:
    """Most spans only carry a subset of the LK attributes. The helper
    must not write empty / None metadata keys when a source is missing —
    that just clutters the LangSmith sidebar with empty fields."""
    processor = _make_processor()
    span = _make_span("eou_detection", {"lk.eou.probability": 0.5})

    processor.on_end(span)

    # eou_detection has no agent_name, ttft, etc. — those keys must
    # NOT appear in the output.
    assert "langsmith.metadata.agent" not in span._attributes
    assert "langsmith.metadata.ttft_ms" not in span._attributes
    # But the one that IS present must have been propagated.
    assert span._attributes["langsmith.metadata.eou_probability"] == "0.5"
