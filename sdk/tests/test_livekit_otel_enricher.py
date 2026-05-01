"""Tests for the LOCAL DELTA branches we added to the vendored
:class:`LiveKitOtelEnricher` for LiveKit-specific span types.

The enricher (``sdk/src/sdk/livekit_otel_enricher.py``) is
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

from sdk.livekit_otel_enricher import LiveKitOtelEnricher, remember_live_trace_call_metadata


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


def _make_processor() -> LiveKitOtelEnricher:
    """Enricher with a no-op downstream so we can inspect the span state
    without an HTTP export firing."""
    downstream = MagicMock()
    return LiveKitOtelEnricher(downstream_processor=downstream)


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


def test_llm_span_writes_canonical_langsmith_io_fields() -> None:
    """LangSmith's OTel mapper reads canonical input/output attributes.
    Keep the legacy gen_ai.prompt/completion fields too, but also write
    input.value/output.value and llm.*_messages so runs render real IO."""
    processor = _make_processor()
    span = _make_span(
        "llm_node",
        {
            "lk.chat_ctx": '{"items": [{"type": "message", "role": "user", "content": "hello"}]}',
            "lk.response.text": "hi there",
        },
    )

    processor.on_end(span)

    assert '"hello"' in span._attributes["input.value"]
    assert '"hi there"' in span._attributes["output.value"]
    assert "llm.input_messages" in span._attributes
    assert "llm.output_messages" in span._attributes


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

    assert span._attributes["langsmith.metadata.ttft_ms"] == "450.3"


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

    assert span._attributes["langsmith.metadata.endpointing_delay_ms"] == "1000.0"
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

    assert span._attributes["langsmith.metadata.e2e_latency_ms"] == "2860.0"


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


def test_lk_job_id_surfaced_as_metadata() -> None:
    """`lk.job_id` is the LiveKit per-call identifier. We expose it
    as `langsmith.metadata.lk_job_id` so it's queryable in LangSmith
    and joinable against agent / gateway logs that also key by job_id.

    We deliberately do NOT set `langsmith.trace.session_id` from
    job_id — that field is a foreign key into LangSmith's session
    table; populating it with a value LangSmith hasn't created itself
    returns HTTP 404 and drops the entire span batch on the floor.
    Thread grouping happens via `langsmith.metadata.thread_id` instead."""
    processor = _make_processor()
    span = _make_span(
        "agent_session",
        {"lk.job_id": "AJ_xyz123", "lk.agent_name": "phone-nyla"},
    )

    processor.on_end(span)

    assert span._attributes["langsmith.metadata.lk_job_id"] == "AJ_xyz123"
    # The trace.session_id field MUST NOT be set — see HTTP 404 above.
    assert "langsmith.trace.session_id" not in span._attributes


def test_thread_id_groups_spans_in_same_trace_without_call_id() -> None:
    """When no call/session id is available, fall back to the OTel trace id."""
    processor = _make_processor()
    span_a = _make_span("user_turn", {"lk.user_transcript": "hi"})
    span_b = _make_span("agent_session", {})
    # Both spans share the default trace_id from _make_span (0xDEADBEEFCAFE).

    processor.on_end(span_a)
    processor.on_end(span_b)

    assert (
        span_a._attributes["langsmith.metadata.thread_id"]
        == span_b._attributes["langsmith.metadata.thread_id"]
    )


def test_thread_id_prefers_call_sid_over_trace_id() -> None:
    """LangSmith threads should model the call, not each short-lived trace.

    Curated conversation/tool spans intentionally close quickly so they appear
    live while the call is still active. They must still share a thread by
    stable call_sid.
    """
    processor = _make_processor()
    span_a = _make_span(
        "user_message",
        {
            "langsmith.span.tags": "conversation,role:user",
            "langsmith.metadata.call_sid": "SCL_call1",
        },
    )
    span_b = _make_span(
        "assistant_message",
        {
            "langsmith.span.tags": "conversation,role:assistant",
            "langsmith.metadata.call_sid": "SCL_call1",
        },
    )
    span_b.context.trace_id = 0xFEEDFACE

    processor.on_end(span_a)
    processor.on_end(span_b)

    assert span_a._attributes["langsmith.metadata.thread_id"] == "SCL_call1"
    assert span_b._attributes["langsmith.metadata.thread_id"] == "SCL_call1"


def test_call_thread_id_propagates_to_livekit_child_spans() -> None:
    """Timed LiveKit child spans must land in the same call thread as curated runs."""
    processor = _make_processor()
    root = _make_span(
        "agent_session",
        {
            "langsmith.metadata.call_sid": "SCL_call1",
            "langsmith.metadata.agent": "phone-nyla",
            "lk.job_id": "AJ_job1",
        },
    )
    root.parent = object()
    child = _make_span("agent_turn", {"lk.e2e_latency": 1.25})
    child.parent = object()

    processor.on_end(root)
    processor.on_end(child)

    assert child._attributes["langsmith.metadata.thread_id"] == "SCL_call1"
    assert child._attributes["langsmith.metadata.call_sid"] == "SCL_call1"
    assert child._attributes["langsmith.metadata.agent"] == "phone-nyla"
    assert child._attributes["langsmith.metadata.e2e_latency_ms"] == "1250.0"


def test_live_call_thread_id_propagates_before_session_root_ends() -> None:
    """Child spans end during the call, before agent_session closes at hangup."""
    processor = _make_processor()
    child = _make_span("agent_turn", {"lk.job_id": "AJ_job1", "lk.e2e_latency": 1.25})
    child.context.trace_id = 0x12345
    remember_live_trace_call_metadata(
        child.context.trace_id,
        {
            "langsmith.metadata.call_sid": "SCL_call1",
            "langsmith.metadata.agent": "phone-nyla",
        },
    )

    processor.on_end(child)

    assert child._attributes["langsmith.metadata.thread_id"] == "SCL_call1"
    assert child._attributes["langsmith.metadata.call_sid"] == "SCL_call1"
    assert child._attributes["langsmith.metadata.agent"] == "phone-nyla"


def test_conversation_id_does_not_set_fake_parent_span_id() -> None:
    """Do not write malformed LangSmith parent ids.

    OTel parentage is carried by real span context. LangSmith thread grouping is
    carried by metadata.thread_id. A literal "conversation" parent id is neither.
    """
    processor = _make_processor()
    child = _make_span("llm_node", {"lk.response.text": "hello", "lk.chat_ctx": "{}"})
    trace_id = format(child.context.trace_id, "032x")
    processor.trace_to_conversation_id[trace_id] = "conv-1"

    processor.on_end(child)

    assert child._attributes["conversation.id"] == "conv-1"
    assert "langsmith.parent_span_id" not in child._attributes


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


def test_token_usage_propagates_for_cost_analysis() -> None:
    """Token-usage attributes are how we attribute spend per call /
    per agent / per turn. Both the OTel-standard `gen_ai.usage.*` and
    the LiveKit-flavoured `lk.agents.usage.*` must surface as metadata
    so cost queries work regardless of which version of LiveKit
    emitted the span."""
    processor = _make_processor()
    span = _make_span(
        "llm_node",
        {
            "gen_ai.usage.input_tokens": 1500,
            "gen_ai.usage.output_tokens": 220,
            "gen_ai.usage.input_audio_tokens": 800,
            "gen_ai.usage.input_cached_tokens": 600,
            "lk.agents.usage.tts_characters": 145,
            "lk.agents.usage.stt_audio_duration": 4.2,
        },
    )

    processor.on_end(span)

    md = span._attributes
    assert md["langsmith.metadata.usage.input_tokens"] == "1500"
    assert md["langsmith.metadata.usage.output_tokens"] == "220"
    assert md["langsmith.metadata.usage.input_audio_tokens"] == "800"
    assert md["langsmith.metadata.usage.input_cached_tokens"] == "600"
    assert md["langsmith.metadata.usage.tts_characters"] == "145"
    assert md["langsmith.metadata.usage.stt_audio_duration_s"] == "4.2"


def test_interruption_metadata_propagates() -> None:
    """LiveKit emits a rich interruption block when the user talks over
    the agent. These are the metrics that answer "why does Aoi keep
    cutting me off" — surfacing them is required for that diagnosis."""
    processor = _make_processor()
    span = _make_span(
        "agent_speaking",
        {
            "lk.is_interruption": True,
            "lk.interruption.detection_delay": 0.34,
            "lk.interruption.prediction_duration": 0.12,
            "lk.interruption.probability": 0.91,
            "lk.interruption.total_duration": 0.46,
        },
    )

    processor.on_end(span)

    md = span._attributes
    assert md["langsmith.metadata.is_interruption"] == "True"
    assert md["langsmith.metadata.interruption_detection_delay_ms"] == "340.0"
    assert md["langsmith.metadata.interruption_probability"] == "0.91"


def test_turn_latency_block_propagates() -> None:
    """The newer `lk.agents.turn.*` latency attrs are emitted by current
    LiveKit on the agent_session span and contain the full per-turn
    latency breakdown. Must surface so the LangSmith sidebar is the
    one place to answer "where did this turn spend its time"."""
    processor = _make_processor()
    span = _make_span(
        "agent_session",
        {
            "lk.agents.turn.e2e_latency": 1.8,
            "lk.agents.turn.llm_ttft": 0.42,
            "lk.agents.turn.tts_ttfb": 0.18,
            "lk.agents.turn.transcription_delay": 0.21,
            "lk.agents.turn.on_user_turn_completed_delay": 0.05,
            "lk.agents.connection.acquire_time": 0.003,
        },
    )

    processor.on_end(span)

    md = span._attributes
    assert md["langsmith.metadata.turn_e2e_latency_ms"] == "1800.0"
    assert md["langsmith.metadata.turn_llm_ttft_ms"] == "420.0"
    assert md["langsmith.metadata.turn_tts_ttfb_ms"] == "180.0"
    assert md["langsmith.metadata.proc_acquire_time_ms"] == "3.0"


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


# ---------------------------------------------------------------------------
# Evaluator-input enrichment
#
# The recall-accuracy-judge and tool-choice-judge LLM evaluators map
# four/five inputs from each function_tool run: user_question, tool_name,
# tool_result, agent_response (+ agent_name). The processor is the only
# place these can be assembled because they span multiple LiveKit spans
# in one turn. These tests pin the contract: every function_tool span
# the processor exports MUST carry these fields, ready for the evaluator
# runtime to pick them up at scoring time.
# ---------------------------------------------------------------------------


def _set_trace_id(span: MagicMock, trace_id: int) -> None:
    """Override the default trace id so multi-span tests can simulate
    spans from the same trace vs different traces."""
    span.context.trace_id = trace_id


def test_user_turn_captures_user_question_for_subsequent_function_tool() -> None:
    """The latest user_turn transcript must be picked up by the next
    function_tool span as `langsmith.metadata.user_question` — that's
    the input the recall-accuracy-judge reads to know what the user
    actually asked before the tool fired."""
    processor = _make_processor()
    trace_id = 0xAAAA1111

    user_turn = _make_span(
        "user_turn",
        {"lk.user_transcript": "do you remember the prank we discussed?"},
    )
    _set_trace_id(user_turn, trace_id)
    processor.on_end(user_turn)

    tool_span = _make_span(
        "function_tool",
        {
            "lk.function_tool.name": "musubi_search",
            "lk.function_tool.arguments": '{"query": "prank"}',
            "lk.function_tool.output": "Found cocoa-pods row",
        },
    )
    _set_trace_id(tool_span, trace_id)
    processor.on_end(tool_span)

    assert (
        tool_span._attributes["langsmith.metadata.user_question"]
        == "do you remember the prank we discussed?"
    )


def test_function_tool_span_writes_tool_result_metadata() -> None:
    """`langsmith.metadata.tool_result` must mirror the tool output so
    the evaluator's variable_mapping can pull it from `extra.metadata`
    rather than relying on `outputs.output` (which the run schema
    populates from gen_ai.completion — fine, but a metadata mirror
    is more robust)."""
    processor = _make_processor()
    span = _make_span(
        "function_tool",
        {
            "lk.function_tool.name": "musubi_recent",
            "lk.function_tool.arguments": "{}",
            "lk.function_tool.output": "row1\nrow2\nrow3",
        },
    )

    processor.on_end(span)

    assert span._attributes["langsmith.metadata.tool_result"] == "row1\nrow2\nrow3"


def test_function_tool_span_exports_immediately() -> None:
    """Tool spans must export the moment LiveKit closes them. The
    earlier ``deferred_tool_spans`` mechanism existed only to backfill
    ``agent_response`` for LangSmith's recall-accuracy-judge; once we
    standardized on SigNoz the deferral was pure latency tax."""
    downstream = MagicMock()
    processor = LiveKitOtelEnricher(downstream_processor=downstream)
    trace_id = 0xBBBB2222

    span = _make_span(
        "function_tool",
        {
            "lk.function_tool.name": "musubi_search",
            "lk.function_tool.arguments": "{}",
            "lk.function_tool.output": "ok",
        },
    )
    _set_trace_id(span, trace_id)
    processor.on_end(span)

    end_calls = [c for c in downstream.on_end.call_args_list if c.args[0] is span]
    assert len(end_calls) == 1, "function_tool span should export immediately"


def test_processor_no_longer_tracks_assistant_response_state() -> None:
    """The ``latest_assistant_response`` mapping was a side-channel
    feeding the deferred-tool-spans release. After the SigNoz refactor
    that state is gone — there's no in-flight bookkeeping to leak."""
    processor = _make_processor()
    assert not hasattr(processor, "latest_assistant_response")
    assert not hasattr(processor, "deferred_tool_spans")


def test_function_tool_carries_user_question_metadata() -> None:
    """``langsmith.metadata.user_question`` and ``openclaw.user_question``
    still get stamped onto every tool span so operators can filter
    "every musubi_search where the user asked X" in any backend."""
    processor = _make_processor()
    trace_id = 0xCCCC3333

    user_turn = _make_span("user_turn", {"lk.user_transcript": "what's the weather?"})
    _set_trace_id(user_turn, trace_id)
    processor.on_end(user_turn)

    tool_span = _make_span(
        "function_tool",
        {
            "lk.function_tool.name": "weather_lookup",
            "lk.function_tool.arguments": "{}",
            "lk.function_tool.output": "72F sunny",
        },
    )
    _set_trace_id(tool_span, trace_id)
    processor.on_end(tool_span)

    assert tool_span._attributes["langsmith.metadata.user_question"] == "what's the weather?"
    assert tool_span._attributes["openclaw.user_question"] == "what's the weather?"


def test_user_turn_then_tool_span_then_job_end_all_export_independently() -> None:
    """Smoke-check the end-to-end flow on the new immediate-export
    path: user_turn, function_tool, llm_node, and job span each fan
    through ``downstream.on_end`` exactly once with no holdback."""
    downstream = MagicMock()
    processor = LiveKitOtelEnricher(downstream_processor=downstream)
    trace_id = 0xEEEE5555

    user_turn = _make_span("user_turn", {"lk.user_transcript": "save this for me"})
    _set_trace_id(user_turn, trace_id)
    processor.on_end(user_turn)

    tool_span = _make_span(
        "function_tool",
        {
            "lk.function_tool.name": "musubi_remember",
            "lk.function_tool.arguments": '{"content": "x"}',
            "lk.function_tool.output": "saved",
        },
    )
    _set_trace_id(tool_span, trace_id)
    processor.on_end(tool_span)

    llm_span = _make_span(
        "llm_node",
        {"lk.chat_ctx": "{}", "lk.response.text": "saved it for you"},
    )
    _set_trace_id(llm_span, trace_id)
    processor.on_end(llm_span)

    job_span = _make_span("job", {"lk.job_id": "AJ_x"})
    _set_trace_id(job_span, trace_id)
    job_span.parent = None
    processor.on_end(job_span)

    exported = [c.args[0] for c in downstream.on_end.call_args_list]
    assert tool_span in exported, "tool span must export"
    assert user_turn in exported, "user_turn span must export"
    assert llm_span in exported, "llm_node span must export"


def test_function_tool_error_path_writes_tool_error_metadata() -> None:
    """The tool-error-flag code evaluator reads
    `langsmith.metadata.tool_error == "true"` to score 1.0. The
    processor must set this when `lk.function_tool.is_error` is
    True — otherwise the eval can never score errors."""
    processor = _make_processor()
    span = _make_span(
        "function_tool",
        {
            "lk.function_tool.name": "musubi_get",
            "lk.function_tool.arguments": "{}",
            "lk.function_tool.output": "not implemented",
            "lk.function_tool.is_error": True,
        },
    )

    processor.on_end(span)

    assert span._attributes["langsmith.metadata.tool_error"] == "true"


def test_user_question_does_not_leak_across_traces() -> None:
    """user_question state is keyed by trace_id. A function_tool span
    in trace B must NOT pick up user_question from trace A."""
    processor = _make_processor()

    user_turn_a = _make_span("user_turn", {"lk.user_transcript": "trace A question"})
    _set_trace_id(user_turn_a, 0xAAAA)
    processor.on_end(user_turn_a)

    tool_b = _make_span(
        "function_tool",
        {
            "lk.function_tool.name": "musubi_search",
            "lk.function_tool.arguments": "{}",
            "lk.function_tool.output": "result",
        },
    )
    _set_trace_id(tool_b, 0xBBBB)  # different trace
    processor.on_end(tool_b)

    assert "langsmith.metadata.user_question" not in tool_b._attributes
