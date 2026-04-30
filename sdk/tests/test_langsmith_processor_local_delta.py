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


def test_session_id_links_to_job_id_as_uuid() -> None:
    """`langsmith.trace.session_id` makes the LangSmith Threads view
    group all turns from one call. LiveKit's `lk.job_id` is the only
    per-call identifier we have, but LangSmith requires the field to
    be a valid UUID — raw `AJ_xyz123` is rejected with HTTP 422 and
    the entire span batch is dropped (which we caught the hard way
    after PR #15 silently broke ingest). Use a deterministic uuid5
    so the value is a valid UUID *and* every turn in the same call
    maps to the same UUID, preserving Thread grouping. The raw
    job_id remains queryable as `langsmith.metadata.lk_job_id`."""
    import uuid as _uuid

    processor = _make_processor()
    span = _make_span(
        "agent_session",
        {"lk.job_id": "AJ_xyz123", "lk.agent_name": "phone-nyla"},
    )

    processor.on_end(span)

    session_id = span._attributes["langsmith.trace.session_id"]
    # Must be a valid UUID — _uuid.UUID raises if not.
    parsed = _uuid.UUID(session_id)
    expected = _uuid.uuid5(_uuid.NAMESPACE_URL, "livekit-job:AJ_xyz123")
    assert parsed == expected, "session_id must be deterministic per job_id"
    # Raw job_id still surfaced as queryable metadata.
    assert span._attributes["langsmith.metadata.lk_job_id"] == "AJ_xyz123"


def test_session_id_uuid_is_stable_across_turns() -> None:
    """Two spans from the same call (same lk.job_id) must produce the
    same session_id UUID, otherwise Thread grouping in LangSmith UI
    would split a single call across multiple Threads."""
    processor = _make_processor()
    span_a = _make_span("user_turn", {"lk.job_id": "AJ_call1", "lk.user_transcript": "hi"})
    span_b = _make_span("agent_session", {"lk.job_id": "AJ_call1"})

    processor.on_end(span_a)
    processor.on_end(span_b)

    assert (
        span_a._attributes["langsmith.trace.session_id"]
        == span_b._attributes["langsmith.trace.session_id"]
    )


def test_session_id_uuid_differs_across_calls() -> None:
    """Two different calls (different lk.job_id values) must produce
    different session_id UUIDs so Threads stay separated."""
    processor = _make_processor()
    span_a = _make_span("agent_session", {"lk.job_id": "AJ_call1"})
    span_b = _make_span("agent_session", {"lk.job_id": "AJ_call2"})

    processor.on_end(span_a)
    processor.on_end(span_b)

    assert (
        span_a._attributes["langsmith.trace.session_id"]
        != span_b._attributes["langsmith.trace.session_id"]
    )


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
    assert md["langsmith.metadata.interruption_detection_delay_ms"] == "0.34"
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
    assert md["langsmith.metadata.turn_e2e_latency_ms"] == "1.8"
    assert md["langsmith.metadata.turn_llm_ttft_ms"] == "0.42"
    assert md["langsmith.metadata.turn_tts_ttfb_ms"] == "0.18"
    assert md["langsmith.metadata.proc_acquire_time_ms"] == "0.003"


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


def test_function_tool_span_is_deferred_until_assistant_response_resolves() -> None:
    """The function_tool span must NOT export immediately — it has to
    wait for the next llm_node to capture agent_response. Otherwise
    the evaluator gets a function_tool run with empty agent_response
    every time."""
    downstream = MagicMock()
    processor = LangSmithSpanProcessor(downstream_processor=downstream)
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

    # downstream.on_end was NOT called for the function_tool span yet
    end_calls = [c for c in downstream.on_end.call_args_list if c.args[0] is span]
    assert end_calls == [], "function_tool span should be deferred, not exported"


def test_llm_node_tracks_latest_assistant_response_for_release() -> None:
    """The processor must remember the most recent llm_node text per
    trace so it can backfill agent_response on deferred tool spans
    when the turn ends."""
    processor = _make_processor()
    trace_id = 0xCCCC3333

    span = _make_span(
        "llm_node",
        {
            "lk.chat_ctx": '{"items": [{"type": "message", "role": "user", "content": "x"}]}',
            "lk.response.text": "the schedule is X",
        },
    )
    _set_trace_id(span, trace_id)
    processor.on_end(span)

    trace_id_hex = format(trace_id, "032x")
    assert processor.latest_assistant_response[trace_id_hex] == "the schedule is X"


def test_next_user_turn_releases_deferred_tool_with_agent_response() -> None:
    """On the next user_turn START (signal that the previous turn
    fully resolved), any deferred tool spans must be released with
    the latest tracked LLM output as agent_response, then exported."""
    downstream = MagicMock()
    processor = LangSmithSpanProcessor(downstream_processor=downstream)
    trace_id = 0xDDDD4444

    # Turn 1: user_turn, function_tool (deferred), llm_node
    user_turn_1 = _make_span("user_turn", {"lk.user_transcript": "what's my favorite band?"})
    _set_trace_id(user_turn_1, trace_id)
    processor.on_end(user_turn_1)

    tool_span = _make_span(
        "function_tool",
        {
            "lk.function_tool.name": "musubi_search",
            "lk.function_tool.arguments": '{"query": "favorite band"}',
            "lk.function_tool.output": "Gojira",
        },
    )
    _set_trace_id(tool_span, trace_id)
    processor.on_end(tool_span)

    llm_span = _make_span(
        "llm_node",
        {"lk.chat_ctx": "{}", "lk.response.text": "Your favorite band is Gojira."},
    )
    _set_trace_id(llm_span, trace_id)
    processor.on_end(llm_span)

    # Turn 2 begins — this should trigger release of the deferred tool span
    user_turn_2 = _make_span("user_turn", {"lk.user_transcript": "and my Tesla?"})
    _set_trace_id(user_turn_2, trace_id)
    processor.on_start(user_turn_2)

    assert (
        tool_span._attributes["langsmith.metadata.agent_response"]
        == "Your favorite band is Gojira."
    )
    end_calls = [c for c in downstream.on_end.call_args_list if c.args[0] is tool_span]
    assert len(end_calls) == 1, "deferred tool span should be exported exactly once"


def test_job_span_end_flushes_deferred_tool_spans() -> None:
    """If the call ends without a follow-up user_turn (final turn of
    the call), the deferred tool spans must still get exported on job
    cleanup — using whatever assistant_response we have, even if empty."""
    downstream = MagicMock()
    processor = LangSmithSpanProcessor(downstream_processor=downstream)
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

    # Job span ends — no next user_turn ever fires
    job_span = _make_span("job", {"lk.job_id": "AJ_x"})
    _set_trace_id(job_span, trace_id)
    job_span.parent = None
    processor.on_end(job_span)

    end_calls = [c for c in downstream.on_end.call_args_list if c.args[0] is tool_span]
    assert len(end_calls) == 1, "deferred tool span should be flushed on job end"
    assert tool_span._attributes["langsmith.metadata.agent_response"] == "saved it for you"


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
