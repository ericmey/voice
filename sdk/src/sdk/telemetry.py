"""Per-call telemetry capture — structured event + metrics logging.

Hooks into AgentSession events to capture:
- Per-turn latency (e2e, LLM TTFT, TTS TTFB, transcription delay)
- User/agent state transitions with timestamps
- Overlapping speech and interruption data
- VAD-level inference stats (when available)
- Tool execution timing
- Token usage breakdown
- Session-level summary stats

Writes structured JSON to ``$LIVEKIT_VOICE_LOGS/call-telemetry/{call_sid}.json``
on session close. If that env var is unset, telemetry capture is a no-op.
"""

from __future__ import annotations

import json
import logging
import os
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Any

from livekit.agents import AgentSession
from livekit.agents.telemetry import tracer as livekit_tracer

from .trace import trace

logger = logging.getLogger("openclaw-livekit.agent")

# OTel tracer for emitting live metric spans alongside the JSON
# telemetry capture. Each session.on(...) hook below uses it to push
# the same data into the OTel pipeline so it lands in LangSmith
# turn-by-turn (within ~1-2s of the event firing) instead of only
# arriving as a postcall summary.
#
# Use LiveKit's dynamic tracer, not the global OpenTelemetry tracer. The
# tracing setup wires this object to the LangSmith provider; using the
# global tracer here can silently no-op while LiveKit spans export fine.
_tracer = livekit_tracer

_SECONDS_TO_MS_METADATA = {
    "e2e_latency": "e2e_latency_ms",
    "llm_node_ttft": "ttft_ms",
    "tts_node_ttfb": "ttfb_ms",
    "transcription_delay": "transcription_delay_ms",
    "end_of_turn_delay": "end_of_turn_delay_ms",
    "on_user_turn_completed_delay": "on_user_turn_completed_delay_ms",
    "ttft": "ttft_ms",
    "duration": "duration_ms",
    "detection_delay": "interruption_detection_delay_ms",
    "prediction_duration": "interruption_prediction_duration_ms",
    "total_duration": "interruption_total_duration_ms",
}

_MODEL_USAGE_FIELDS = (
    "input_tokens",
    "input_cached_tokens",
    "input_cached_audio_tokens",
    "input_cached_text_tokens",
    "input_cached_image_tokens",
    "input_audio_tokens",
    "input_text_tokens",
    "input_image_tokens",
    "output_tokens",
    "output_audio_tokens",
    "output_text_tokens",
    "session_duration",
    "audio_duration",
    "characters_count",
    "total_requests",
)

_TOKEN_TOTAL_FIELDS = (
    "input_tokens",
    "input_cached_tokens",
    "input_cached_audio_tokens",
    "input_cached_text_tokens",
    "input_cached_image_tokens",
    "input_audio_tokens",
    "input_text_tokens",
    "input_image_tokens",
    "output_tokens",
    "output_audio_tokens",
    "output_text_tokens",
)

_TURN_METRIC_FIELDS = (
    "e2e_latency",
    "llm_node_ttft",
    "tts_node_ttfb",
    "transcription_delay",
    "end_of_turn_delay",
    "on_user_turn_completed_delay",
    "started_speaking_at",
    "stopped_speaking_at",
)


def _verbose_langsmith_telemetry_enabled() -> bool:
    """Whether to export low-level event/metric spans to LangSmith.

    The default LangSmith view should read like the call: user turns,
    assistant turns, tool calls, errors, and a compact session summary.
    Full state/latency chatter stays in the local telemetry JSON unless
    this flag is enabled for a focused debugging session.
    """
    return os.environ.get("LANGSMITH_VERBOSE_TELEMETRY", "").lower() in ("true", "1", "yes")


def _message_payload(role: str, text: str) -> str:
    return json.dumps({"messages": [{"role": role, "content": text}]}, ensure_ascii=False)


def _metadata_scalar(value: Any) -> str | int | float | bool | None:
    if value is None or value == "":
        return None
    if isinstance(value, bool):
        return str(value)
    if isinstance(value, (str, int, float, bool)):
        return value
    return str(value)


def _set_metadata(span: Any, key: str, value: Any) -> None:
    scalar = _metadata_scalar(value)
    if scalar is None:
        return
    span.set_attribute(f"langsmith.metadata.{key}", scalar)


def _duration_ms_key(key: str) -> str | None:
    if key in _SECONDS_TO_MS_METADATA:
        return _SECONDS_TO_MS_METADATA[key]
    if key.endswith("_duration"):
        return f"{key}_ms"
    return None


def _usage_value(value: Any) -> Any:
    return round(value, 4) if isinstance(value, float) else value


def _model_usage_entry(mu: Any) -> dict[str, Any]:
    entry: dict[str, Any] = {
        "type": type(mu).__name__,
        "provider": getattr(mu, "provider", None),
        "model": getattr(mu, "model", None),
    }
    for field in _MODEL_USAGE_FIELDS:
        val = getattr(mu, field, None)
        if val is not None:
            entry[field] = _usage_value(val)
    return {key: value for key, value in entry.items() if value is not None}


@contextmanager
def _start_live_langsmith_span(name: str, *, duration_seconds: float | None = None):
    """Create a short-lived root trace for live LangSmith thread updates.

    When ``duration_seconds`` is provided, the span's start time is backdated
    so the LangSmith Runs row reflects the actual generation/turn duration
    instead of an instantaneous mirror event. This is what surfaces real
    latency on ``user_message`` / ``assistant_message`` / tool runs.
    """
    start_time_ns: int | None = None
    if duration_seconds is not None and duration_seconds > 0:
        try:
            start_time_ns = time.time_ns() - int(float(duration_seconds) * 1_000_000_000)
        except (TypeError, ValueError, OverflowError):
            start_time_ns = None

    try:
        from opentelemetry.context import Context

        kwargs: dict[str, Any] = {"context": Context()}
        if start_time_ns is not None:
            kwargs["start_time"] = start_time_ns
        with _tracer.start_as_current_span(name, **kwargs) as span:
            yield span
    except TypeError:
        with _tracer.start_as_current_span(name) as span:
            yield span


def _set_span_io(
    span: Any,
    *,
    input_role: str | None = None,
    input_text: str | None = None,
    output_role: str | None = None,
    output_text: str | None = None,
) -> None:
    if input_text:
        payload = _message_payload(input_role or "user", input_text)
        span.set_attribute("inputs", payload)
        span.set_attribute("input.value", payload)
        span.set_attribute("gen_ai.prompt.0.role", input_role or "user")
        span.set_attribute("gen_ai.prompt.0.content", input_text)
    if output_text:
        payload = _message_payload(output_role or "assistant", output_text)
        span.set_attribute("outputs", payload)
        span.set_attribute("output.value", payload)
        span.set_attribute("gen_ai.completion.0.role", output_role or "assistant")
        span.set_attribute("gen_ai.completion.0.content", output_text)


def emit_conversation_span(
    *,
    call_sid: str | None,
    agent_name: str,
    role: str,
    text: str,
    metrics: dict[str, Any] | None = None,
    usage: dict[str, Any] | None = None,
    model: str | None = None,
    provider: str | None = None,
) -> None:
    """Emit a first-class LangSmith span for one visible transcript item.

    Latency: assistant turns backdate the span to ``e2e_latency`` so the
    LangSmith Runs row shows real duration; user transcripts use
    ``transcription_delay`` similarly.

    Tokens / cost: when ``usage`` (per-turn realtime metrics) and
    ``model`` are provided, ``gen_ai.usage.*`` numeric attributes plus
    ``gen_ai.system`` / ``gen_ai.request.model`` get set so the LangSmith
    Runs view fills the Tokens, First Token, and Cost columns.
    """
    if not call_sid or not text.strip():
        return

    clean_role = role if role in {"user", "assistant", "system", "tool"} else "assistant"
    span_name = f"{clean_role}_message"

    duration_seconds: float | None = None
    if isinstance(metrics, dict):
        if clean_role == "user":
            duration_seconds = _coerce_seconds(metrics.get("transcription_delay"))
        else:
            duration_seconds = _coerce_seconds(metrics.get("e2e_latency"))

    # Assistant turns are recorded as ``llm`` so LangSmith populates the
    # Tokens / Cost / First Token columns on the Runs view from the
    # gen_ai.usage.* attributes. User and tool turns stay as ``chain``.
    # We mirror the kind as ``openinference.span.kind`` (Phoenix /
    # Arize convention) so OpenInference-aware backends categorize
    # the span correctly without LangSmith-specific attributes.
    span_kind = "llm" if clean_role == "assistant" else "chain"
    openinference_kind = "LLM" if clean_role == "assistant" else "CHAIN"
    with _start_live_langsmith_span(span_name, duration_seconds=duration_seconds) as span:
        span.set_attribute("langsmith.span.kind", span_kind)
        span.set_attribute("openinference.span.kind", openinference_kind)
        span.set_attribute("langsmith.span.tags", f"conversation,role:{clean_role}")
        span.set_attribute("langsmith.metadata.call_sid", call_sid)
        span.set_attribute("langsmith.metadata.agent", agent_name)
        span.set_attribute("langsmith.metadata.role", clean_role)
        span.set_attribute("langsmith.metadata.text_length", len(text))
        # Vendor-neutral mirrors visible to any OTel backend.
        span.set_attribute("openclaw.call_sid", call_sid)
        span.set_attribute("openclaw.agent", agent_name)
        span.set_attribute("openclaw.role", clean_role)
        if clean_role == "assistant":
            # gen_ai.operation.name=chat tells the LangSmith OTel mapper this
            # run is a chat completion; combined with span_kind=llm above the
            # run type becomes ``llm``.
            span.set_attribute("gen_ai.operation.name", "chat")
        for key in _TURN_METRIC_FIELDS:
            value = metrics.get(key) if isinstance(metrics, dict) else None
            _set_metadata(span, key, value)
            if isinstance(value, (int, float)):
                ms_key = _duration_ms_key(key)
                if ms_key:
                    _set_metadata(span, ms_key, round(float(value) * 1000, 3))

        if clean_role == "assistant":
            _apply_genai_attrs(span, usage=usage, metrics=metrics, model=model, provider=provider)

        if clean_role == "user":
            _set_span_io(span, input_role="user", input_text=text)
        elif clean_role == "tool":
            _set_span_io(span, output_role="tool", output_text=text)
        else:
            _set_span_io(span, output_role="assistant", output_text=text)


def _coerce_seconds(value: Any) -> float | None:
    if value is None:
        return None
    try:
        seconds = float(value)
    except (TypeError, ValueError):
        return None
    if seconds <= 0:
        return None
    return seconds


def _apply_genai_attrs(
    span: Any,
    *,
    usage: dict[str, Any] | None = None,
    metrics: dict[str, Any] | None = None,
    model: str | None = None,
    provider: str | None = None,
) -> None:
    """Populate ``gen_ai.*`` attributes LangSmith reads for tokens/cost/TTFT."""
    if model:
        span.set_attribute("gen_ai.request.model", str(model))
        span.set_attribute("gen_ai.response.model", str(model))
        span.set_attribute("langsmith.metadata.ls_model_name", str(model))
    if provider:
        span.set_attribute("gen_ai.system", str(provider))
        span.set_attribute("langsmith.metadata.ls_provider", str(provider))

    if isinstance(usage, dict) and usage:
        input_tokens = _coerce_int(usage.get("input_tokens"))
        output_tokens = _coerce_int(usage.get("output_tokens"))
        total_tokens = _coerce_int(usage.get("total_tokens"))
        if total_tokens is None and input_tokens is not None and output_tokens is not None:
            total_tokens = input_tokens + output_tokens
        if input_tokens is not None:
            span.set_attribute("gen_ai.usage.input_tokens", input_tokens)
            span.set_attribute("gen_ai.usage.prompt_tokens", input_tokens)
        if output_tokens is not None:
            span.set_attribute("gen_ai.usage.output_tokens", output_tokens)
            span.set_attribute("gen_ai.usage.completion_tokens", output_tokens)
        if total_tokens is not None:
            span.set_attribute("gen_ai.usage.total_tokens", total_tokens)
        cached_tokens = _coerce_int(usage.get("input_cached_tokens"))
        if cached_tokens is not None:
            span.set_attribute("gen_ai.usage.input_cached_tokens", cached_tokens)
        audio_input = _coerce_int(usage.get("input_audio_tokens"))
        if audio_input is not None:
            span.set_attribute("gen_ai.usage.input_audio_tokens", audio_input)
        audio_output = _coerce_int(usage.get("output_audio_tokens"))
        if audio_output is not None:
            span.set_attribute("gen_ai.usage.output_audio_tokens", audio_output)

    ttft = None
    if isinstance(metrics, dict):
        ttft = _coerce_seconds(metrics.get("llm_node_ttft")) or _coerce_seconds(metrics.get("ttft"))
    if ttft is not None:
        span.set_attribute("gen_ai.server.time_to_first_token", ttft)
        span.set_attribute("gen_ai.client.operation.time_to_first_chunk", ttft)
        # LangSmith fills the "First Token" column from a ``new_token`` span event
        # whose timestamp is when the first chunk arrived. Emit a single event
        # at start_time + ttft so the column populates from the OTel pipeline.
        try:
            span_ctx = span.get_span_context()  # type: ignore[attr-defined]
            start_time_ns = getattr(span, "start_time", None) or getattr(span, "_start_time", None)
            if start_time_ns and span_ctx and span_ctx.is_valid:
                event_ns = int(start_time_ns) + int(ttft * 1_000_000_000)
                span.add_event("new_token", attributes={"chunk_index": 0}, timestamp=event_ns)
        except Exception:
            pass


def _coerce_int(value: Any) -> int | None:
    if value is None or value == "":
        return None
    try:
        return int(float(value))
    except (TypeError, ValueError):
        return None


def _emit_tool_span(
    *,
    call_sid: str,
    agent_name: str,
    call: Any,
    output: Any,
    duration_seconds: float | None = None,
) -> None:
    """Emit a first-class LangSmith tool run from LiveKit's executed-tools event."""
    tool_name = str(getattr(call, "name", "unknown_tool") or "unknown_tool")
    tool_args = str(getattr(call, "arguments", "") or "")
    tool_call_id = str(getattr(call, "call_id", "") or getattr(call, "id", "") or "")
    tool_output = getattr(output, "output", output)
    is_error = bool(getattr(output, "is_error", False)) if output is not None else True
    output_text = str(tool_output) if tool_output is not None else ""

    with _start_live_langsmith_span(tool_name, duration_seconds=duration_seconds) as span:
        span.set_attribute("langsmith.span.kind", "tool")
        span.set_attribute("openinference.span.kind", "TOOL")
        span.set_attribute(
            "langsmith.span.tags", f"tool:{tool_name}" + (",error" if is_error else "")
        )
        span.set_attribute("langsmith.metadata.call_sid", call_sid)
        span.set_attribute("langsmith.metadata.agent", agent_name)
        span.set_attribute("langsmith.metadata.tool_name", tool_name)
        span.set_attribute("langsmith.metadata.tool_arguments", tool_args)
        span.set_attribute("langsmith.metadata.tool_result", output_text)
        span.set_attribute("langsmith.metadata.tool_source", "function_tools_executed")
        span.set_attribute("openclaw.call_sid", call_sid)
        span.set_attribute("openclaw.agent", agent_name)
        span.set_attribute("openclaw.tool_name", tool_name)
        span.set_attribute("gen_ai.tool.name", tool_name)
        span.set_attribute("gen_ai.tool.input", tool_args)
        span.set_attribute("gen_ai.tool.output", output_text)
        span.set_attribute("gen_ai.operation.name", "execute_tool")
        if tool_call_id:
            span.set_attribute("gen_ai.tool.call.id", tool_call_id)
            span.set_attribute("langsmith.metadata.tool_call_id", tool_call_id)
        if is_error:
            span.set_attribute("langsmith.metadata.tool_error", "true")
            # OTel error semantics — any backend (LangSmith, Phoenix,
            # Datadog) renders this as a failed run with the message.
            try:
                from opentelemetry.trace import Status, StatusCode

                span.set_status(Status(StatusCode.ERROR, output_text[:300] or "tool error"))
                span.set_attribute("error", True)
                span.set_attribute("error.message", output_text[:1000])
            except Exception:
                pass
        _set_span_io(
            span,
            input_role="user",
            input_text=f"call {tool_name}({tool_args})",
            output_role="tool",
            output_text=("[error] " if is_error else "") + output_text,
        )


def _usage_attrs_from_event(ev: Any) -> dict[str, Any]:
    usage = getattr(ev, "usage", None)
    if usage is None:
        return {}
    model_usage = getattr(usage, "model_usage", []) or []
    usage_attrs: dict[str, Any] = {}
    totals: dict[str, int | float] = {}
    primary_model: str | None = None
    primary_provider: str | None = None
    for mu in model_usage:
        mtype = type(mu).__name__
        model = getattr(mu, "model", None) or "unknown"
        provider = getattr(mu, "provider", None) or "unknown"
        if primary_model is None and model != "unknown":
            primary_model = model
        if primary_provider is None and provider != "unknown":
            primary_provider = provider
        prefix = f"{mtype.lower()}.{model}"
        usage_attrs[f"{prefix}.provider"] = provider
        for field in _MODEL_USAGE_FIELDS:
            val = getattr(mu, field, None)
            if val is not None:
                usage_attrs[f"{prefix}.{field}"] = _usage_value(val)
                if field in _TOKEN_TOTAL_FIELDS and isinstance(val, (int, float)):
                    totals[field] = totals.get(field, 0) + val
                if field in {"session_duration", "audio_duration"} and isinstance(
                    val, (int, float)
                ):
                    usage_attrs[f"{prefix}.{field}_ms"] = round(float(val) * 1000, 3)
    for field, value in totals.items():
        usage_attrs[f"usage.{field}"] = _usage_value(value)
    input_tokens = totals.get("input_tokens", 0)
    output_tokens = totals.get("output_tokens", 0)
    if input_tokens or output_tokens:
        usage_attrs["usage.total_tokens"] = _usage_value(input_tokens + output_tokens)
    if primary_model:
        usage_attrs["__primary_model"] = primary_model
    if primary_provider:
        usage_attrs["__primary_provider"] = primary_provider
    return usage_attrs


def _emit_metric_span(name: str, call_sid: str, agent_name: str, attrs: dict[str, Any]) -> None:
    """Emit a short-lived OTel span carrying live LiveKit metric data.

    Each metric event creates one span. The span is started + populated
    + ended synchronously (sub-millisecond), then BatchSpanProcessor
    flushes it to LangSmith on its next 1s tick. End-to-end the data
    shows up in the LangSmith UI within ~1-3 seconds of the underlying
    event firing — that's the "live updates" Eric asked for.

    Async-safe: this runs on the asyncio event loop alongside the
    session.on(...) handlers. Span creation is a few attribute writes,
    not I/O, so it doesn't block the loop.

    All attrs surface as ``langsmith.metadata.*`` keys so the LangSmith
    UI renders them in the span sidebar and they're filterable in
    queries (e.g. ``metadata.ttft_ms > 1000``). Booleans are coerced
    to strings because LangSmith's metadata store treats them as
    strings on render.
    """
    duration_seconds = _coerce_seconds(attrs.get("duration"))
    if duration_seconds is None:
        duration_seconds = _coerce_seconds(attrs.get("duration_seconds"))
    primary_model = attrs.pop("__primary_model", None) if isinstance(attrs, dict) else None
    primary_provider = attrs.pop("__primary_provider", None) if isinstance(attrs, dict) else None

    with _start_live_langsmith_span(name, duration_seconds=duration_seconds) as span:
        span.set_attribute("langsmith.span.kind", "chain")
        # Correlation handles so these synthetic spans can be joined
        # against the trace's other spans by call_sid / agent / job.
        if call_sid:
            span.set_attribute("langsmith.metadata.call_sid", call_sid)
        if agent_name:
            span.set_attribute("langsmith.metadata.agent", agent_name)
        # Tag for filtering — every metric span carries this so
        # operators can do tag:metrics in LangSmith filters.
        span.set_attribute("langsmith.span.tags", f"metrics,{name}")
        for key, value in attrs.items():
            _set_metadata(span, key, value)
            if isinstance(value, (int, float)):
                ms_key = _duration_ms_key(key)
                if ms_key:
                    _set_metadata(span, ms_key, round(float(value) * 1000, 3))

        # session_usage spans should expose tokens + model so the LangSmith
        # Runs view fills the Tokens / Cost columns rather than just metadata.
        if name in {"session_usage", "session_close", "realtime_metrics"}:
            usage_for_genai = {
                "input_tokens": attrs.get("usage.input_tokens"),
                "output_tokens": attrs.get("usage.output_tokens"),
                "total_tokens": attrs.get("usage.total_tokens"),
                "input_cached_tokens": attrs.get("usage.input_cached_tokens"),
                "input_audio_tokens": attrs.get("usage.input_audio_tokens"),
                "output_audio_tokens": attrs.get("usage.output_audio_tokens"),
            }
            if name == "realtime_metrics":
                for legacy_key, mapped_key in (
                    ("input_tokens", "input_tokens"),
                    ("output_tokens", "output_tokens"),
                    ("total_tokens", "total_tokens"),
                ):
                    if usage_for_genai.get(mapped_key) is None:
                        usage_for_genai[mapped_key] = attrs.get(legacy_key)
            _apply_genai_attrs(
                span,
                usage=usage_for_genai,
                metrics=attrs,
                model=primary_model,
                provider=primary_provider,
            )


def _telemetry_dir() -> Path | None:
    """Resolve the telemetry dir from LIVEKIT_VOICE_LOGS, or None."""
    logs = os.environ.get("LIVEKIT_VOICE_LOGS")
    return Path(logs) / "call-telemetry" if logs else None


def _ensure_telemetry_dir() -> Path | None:
    d = _telemetry_dir()
    if d is None:
        return None
    try:
        d.mkdir(parents=True, exist_ok=True)
    except Exception as exc:
        logger.error("telemetry dir creation failed: %s", exc)
        return None
    return d


class TelemetryCollector:
    """Accumulates session events and writes a structured JSON on flush."""

    def __init__(self, call_sid: str, agent_name: str) -> None:
        self.call_sid = call_sid
        self.agent_name = agent_name
        self.started_at = time.time()
        self.started_at_iso = time.strftime("%Y-%m-%dT%H:%M:%S%z")

        # Per-turn latency metrics (one entry per assistant response).
        # Populated for chained pipelines (STT/LLM/TTS) where the framework
        # rolls per-component metrics into ``ChatMessage.metrics``.
        self.turns: list[dict[str, Any]] = []

        # Realtime-model metrics — separate channel for Gemini realtime
        # and similar models. Captured via ``metrics_collected`` events on
        # the session, NOT via the ``metrics`` field on conversation items
        # (which stays empty for realtime). One entry per RealtimeModelMetrics
        # event the model emits.
        self.realtime_metrics: list[dict[str, Any]] = []

        # State transitions
        self.user_states: list[dict[str, Any]] = []
        self.agent_states: list[dict[str, Any]] = []

        # Interruptions and overlapping speech
        self.overlapping_speech: list[dict[str, Any]] = []
        self.false_interruptions: int = 0

        # Tool calls
        self.tool_calls: list[dict[str, Any]] = []

        # Usage (accumulated)
        self.usage_snapshots: list[dict[str, Any]] = []

        # Errors
        self.errors: list[dict[str, Any]] = []

        # Close event
        self.close_reason: str | None = None
        self.close_error: str | None = None

    def record_turn(self, metrics: dict[str, Any], role: str, text: str) -> None:
        """Record per-turn latency from ChatMessage.metrics."""
        entry: dict[str, Any] = {
            "turn_index": len(self.turns),
            "timestamp": time.time() - self.started_at,
            "role": role,
            "text_preview": text[:100] if text else "",
        }
        # Extract the latency fields we care about
        for key in (
            "e2e_latency",
            "llm_node_ttft",
            "tts_node_ttfb",
            "transcription_delay",
            "end_of_turn_delay",
            "on_user_turn_completed_delay",
        ):
            val = metrics.get(key)
            if val is not None:
                entry[key] = round(val, 4)
        self.turns.append(entry)

    def record_realtime_metrics(self, ev: Any) -> None:
        """Capture a ``RealtimeModelMetrics`` event from the session.

        For Gemini realtime / similar bidirectional voice models, the
        framework emits these as ``metrics_collected`` events instead of
        rolling them into ``ChatMessage.metrics``. Without capturing them
        here, Nyla/Aoi calls show entirely null e2e_latency / TTFT
        because the chained-pipeline path in :meth:`record_turn` never
        fires for realtime.

        Fields captured (all from livekit.agents.metrics.RealtimeModelMetrics):
        - ``ttft``: time-to-first-token (closest realtime equivalent of
          chained ``llm_node_ttft``; -1 means no audio token sent).
        - ``duration``: total response duration from created → done.
        - ``input_tokens``/``output_tokens``: usage breakdown.
        """
        entry: dict[str, Any] = {
            "timestamp": time.time() - self.started_at,
            "request_id": getattr(ev, "request_id", None),
        }
        for field in (
            "ttft",
            "duration",
            "session_duration",
            "input_tokens",
            "output_tokens",
            "total_tokens",
            "tokens_per_second",
        ):
            val = getattr(ev, field, None)
            if val is not None:
                entry[field] = round(val, 4) if isinstance(val, float) else val
        for details_field in ("input_token_details", "output_token_details"):
            details = getattr(ev, details_field, None)
            if details is not None:
                entry[details_field] = str(details)
        self.realtime_metrics.append(entry)

    def record_user_state(self, old: str, new: str) -> None:
        self.user_states.append(
            {
                "timestamp": time.time() - self.started_at,
                "old": old,
                "new": new,
            }
        )

    def record_agent_state(self, old: str, new: str) -> None:
        self.agent_states.append(
            {
                "timestamp": time.time() - self.started_at,
                "old": old,
                "new": new,
            }
        )

    def record_overlap(self, event: Any) -> None:
        entry: dict[str, Any] = {
            "timestamp": time.time() - self.started_at,
            "is_interruption": getattr(event, "is_interruption", None),
            "probability": None,
            "detection_delay": None,
            "prediction_duration": None,
            "total_duration": None,
        }
        for field in ("probability", "detection_delay", "prediction_duration", "total_duration"):
            val = getattr(event, field, None)
            if val is not None:
                entry[field] = round(val, 4)
        self.overlapping_speech.append(entry)

    def record_tool_execution(self, event: Any) -> None:
        calls = getattr(event, "function_calls", []) or []
        outputs = getattr(event, "function_call_outputs", []) or []
        for i, call in enumerate(calls):
            name = getattr(call, "name", "unknown")
            output = outputs[i] if i < len(outputs) else None
            tool_output = getattr(output, "output", output)
            is_error = bool(getattr(output, "is_error", False)) if output is not None else True
            self.tool_calls.append(
                {
                    "timestamp": time.time() - self.started_at,
                    "name": name,
                    "call_id": getattr(call, "call_id", None) or getattr(call, "id", None),
                    "arguments": getattr(call, "arguments", "") or "",
                    "output": str(tool_output) if tool_output is not None else "",
                    "is_error": is_error,
                    "success": output is not None and not is_error,
                }
            )

    def record_usage(self, event: Any) -> None:
        usage = getattr(event, "usage", None)
        if not usage:
            return
        model_usage = getattr(usage, "model_usage", []) or []
        snapshot: dict[str, Any] = {
            "timestamp": time.time() - self.started_at,
            "models": [],
        }
        for mu in model_usage:
            snapshot["models"].append(_model_usage_entry(mu))
        self.usage_snapshots.append(snapshot)

    def record_error(self, event: Any) -> None:
        self.errors.append(
            {
                "timestamp": time.time() - self.started_at,
                "error": str(getattr(event, "error", event)),
            }
        )

    def record_close(self, event: Any) -> None:
        self.close_reason = str(getattr(event, "reason", "unknown"))
        self.close_error = str(getattr(event, "error", "")) or None

    def build_summary(self) -> dict[str, Any]:
        """Compute session-level summary stats from accumulated data."""
        duration = time.time() - self.started_at

        # Latency stats — chained pipeline emits per-turn metrics rolled
        # into ``ChatMessage.metrics``; realtime models emit them via a
        # separate ``metrics_collected`` event we capture into
        # ``self.realtime_metrics``. If chained values are absent, fall
        # back to realtime ttft (genuinely the only latency signal we
        # have for realtime — there is no voice-to-voice e2e delivered
        # by the framework on that path). Also keep ttft >= 0 — the
        # framework uses -1 to mean "no audio token sent."
        e2e_values = [t["e2e_latency"] for t in self.turns if "e2e_latency" in t]
        ttft_values = [t["llm_node_ttft"] for t in self.turns if "llm_node_ttft" in t]
        if not e2e_values and self.realtime_metrics:
            # Realtime models: report ttft under the e2e key so downstream
            # consumers (Rin reviews, dashboards) can read one field. Note
            # the source via :func:`flush` so it stays interpretable.
            e2e_values = [m["ttft"] for m in self.realtime_metrics if m.get("ttft", -1) >= 0]
        if not ttft_values and self.realtime_metrics:
            ttft_values = [m["ttft"] for m in self.realtime_metrics if m.get("ttft", -1) >= 0]

        def _stats(values: list[float]) -> dict[str, float | None]:
            if not values:
                return {"min": None, "max": None, "avg": None, "p90": None, "count": 0}
            s = sorted(values)
            p90_idx = int(len(s) * 0.9)
            return {
                "min": round(s[0], 4),
                "max": round(s[-1], 4),
                "avg": round(sum(s) / len(s), 4),
                "p90": round(s[min(p90_idx, len(s) - 1)], 4),
                "count": len(s),
            }

        interruptions = [o for o in self.overlapping_speech if o.get("is_interruption")]
        backchannels = [o for o in self.overlapping_speech if not o.get("is_interruption")]

        return {
            "duration_seconds": round(duration, 1),
            "total_turns": len(self.turns),
            "e2e_latency": _stats(e2e_values),
            "llm_ttft": _stats(ttft_values),
            "interruptions": len(interruptions),
            "false_interruptions": self.false_interruptions,
            "backchannels": len(backchannels),
            "overlapping_speech_events": len(self.overlapping_speech),
            "tool_calls_total": len(self.tool_calls),
            "tool_calls_failed": sum(1 for t in self.tool_calls if not t["success"]),
            "errors": len(self.errors),
        }

    def flush(self) -> Path | None:
        """Write the telemetry JSON to disk. Returns the path or None on failure."""
        d = _ensure_telemetry_dir()
        if d is None:
            return None
        path = d / f"{self.call_sid}.json"

        # Tag the latency source so consumers know whether they're reading
        # voice-to-voice e2e (chained) or ttft-as-e2e-proxy (realtime).
        latency_source = (
            "chained"
            if any("e2e_latency" in t for t in self.turns)
            else ("realtime_ttft" if self.realtime_metrics else "none")
        )

        doc = {
            "call_sid": self.call_sid,
            "agent": self.agent_name,
            "started_at": self.started_at_iso,
            "ended_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
            "close_reason": self.close_reason,
            "close_error": self.close_error,
            "latency_source": latency_source,
            "summary": self.build_summary(),
            "turns": self.turns,
            "realtime_metrics": self.realtime_metrics,
            "user_states": self.user_states,
            "agent_states": self.agent_states,
            "overlapping_speech": self.overlapping_speech,
            "tool_calls": self.tool_calls,
            "usage": self.usage_snapshots[-1] if self.usage_snapshots else None,
            "errors": self.errors,
        }

        try:
            with path.open("w", encoding="utf-8") as f:
                json.dump(doc, f, indent=2)
            logger.info("telemetry written: %s", path)
            trace(f"telemetry written: {path}")
            return path
        except Exception as err:
            logger.error("telemetry write failed: %s", err)
            trace(f"telemetry write failed: {err}")
            return None


def wire_telemetry_capture(
    session: AgentSession,
    call_sid: str | None,
    agent_name: str = "unknown",
) -> TelemetryCollector | None:
    """Register event listeners on *session* that capture structured telemetry.

    Call this BEFORE ``session.start()`` so startup state, greeting, usage,
    and later turns all land in the collector and LangSmith metric spans.
    Returns the collector so callers can access it if needed.
    """
    if not call_sid:
        return None

    collector = TelemetryCollector(call_sid, agent_name)
    latest_usage_attrs: dict[str, Any] = {}
    latest_realtime: dict[str, Any] = {}

    @session.on("conversation_item_added")
    def _on_item(ev: Any) -> None:
        item = getattr(ev, "item", None)
        if item is None:
            return
        role = getattr(item, "role", None) or "unknown"

        text = ""
        if hasattr(item, "text_content"):
            text = item.text_content or ""
        elif hasattr(item, "content"):
            content = item.content
            if isinstance(content, str):
                text = content
            elif isinstance(content, list):
                text = " ".join(c for c in content if isinstance(c, str))

        clean_text = text.strip() if isinstance(text, str) else ""
        if not clean_text:
            return

        metrics = getattr(item, "metrics", None)
        merged_metrics: dict[str, Any] = {}
        if isinstance(metrics, dict):
            merged_metrics.update(metrics)
        if role == "assistant" and latest_realtime:
            for key, value in latest_realtime.items():
                merged_metrics.setdefault(key, value)

        usage_for_span: dict[str, Any] | None = None
        model: str | None = None
        provider: str | None = None
        if role == "assistant":
            usage_for_span = {
                "input_tokens": latest_realtime.get("input_tokens")
                or latest_usage_attrs.get("usage.input_tokens"),
                "output_tokens": latest_realtime.get("output_tokens")
                or latest_usage_attrs.get("usage.output_tokens"),
                "total_tokens": latest_realtime.get("total_tokens")
                or latest_usage_attrs.get("usage.total_tokens"),
                "input_cached_tokens": latest_usage_attrs.get("usage.input_cached_tokens"),
                "input_audio_tokens": latest_usage_attrs.get("usage.input_audio_tokens"),
                "output_audio_tokens": latest_usage_attrs.get("usage.output_audio_tokens"),
            }
            model = latest_realtime.get("model") or latest_usage_attrs.get("__primary_model")
            provider = latest_realtime.get("provider") or latest_usage_attrs.get(
                "__primary_provider"
            )

        emit_conversation_span(
            call_sid=call_sid,
            agent_name=agent_name,
            role=role,
            text=clean_text,
            metrics=merged_metrics or None,
            usage=usage_for_span,
            model=model,
            provider=provider,
        )

        if metrics and role == "assistant":
            collector.record_turn(metrics, role, clean_text)

        if not _verbose_langsmith_telemetry_enabled():
            return

        # Verbose-only: keep the per-turn metric span for deep dives.
        turn_attrs: dict[str, Any] = {
            "turn_index": len(collector.turns) - 1 if collector.turns else 0,
            "role": role,
            "text_preview": clean_text[:200],
        }
        if isinstance(metrics, dict):
            for key in (
                "e2e_latency",
                "llm_node_ttft",
                "tts_node_ttfb",
                "transcription_delay",
                "end_of_turn_delay",
                "on_user_turn_completed_delay",
            ):
                val = metrics.get(key)
                if val is not None:
                    turn_attrs[key] = val
        _emit_metric_span("turn_metrics", call_sid, agent_name, turn_attrs)

    @session.on("user_state_changed")
    def _on_user_state(ev: Any) -> None:
        old = str(getattr(ev, "old_state", "?"))
        new = str(getattr(ev, "new_state", "?"))
        collector.record_user_state(old, new)
        if _verbose_langsmith_telemetry_enabled():
            _emit_metric_span(
                "user_state_changed",
                call_sid,
                agent_name,
                {"old_state": old, "new_state": new},
            )

    @session.on("agent_state_changed")
    def _on_agent_state(ev: Any) -> None:
        old = str(getattr(ev, "old_state", "?"))
        new = str(getattr(ev, "new_state", "?"))
        collector.record_agent_state(old, new)
        if _verbose_langsmith_telemetry_enabled():
            _emit_metric_span(
                "agent_state_changed",
                call_sid,
                agent_name,
                {"old_state": old, "new_state": new},
            )

    @session.on("overlapping_speech")
    def _on_overlap(ev: Any) -> None:
        collector.record_overlap(ev)
        overlap_attrs: dict[str, Any] = {
            "is_interruption": bool(getattr(ev, "is_interruption", False)),
        }
        for field in ("probability", "detection_delay", "prediction_duration", "total_duration"):
            val = getattr(ev, field, None)
            if val is not None:
                overlap_attrs[field] = val
        if _verbose_langsmith_telemetry_enabled():
            _emit_metric_span("overlapping_speech", call_sid, agent_name, overlap_attrs)

    @session.on("agent_false_interruption")
    def _on_false_interrupt(ev: Any) -> None:
        collector.false_interruptions += 1
        if _verbose_langsmith_telemetry_enabled():
            _emit_metric_span(
                "false_interruption",
                call_sid,
                agent_name,
                {"false_interruptions_total": collector.false_interruptions},
            )

    @session.on("function_tools_executed")
    def _on_tools(ev: Any) -> None:
        collector.record_tool_execution(ev)
        # Per-tool span — one event can carry multiple tool calls; emit
        # one OTel span per tool invocation so each shows up
        # independently in LangSmith filters (`tag:tool:musubi_search`).
        calls = getattr(ev, "function_calls", []) or []
        outputs = getattr(ev, "function_call_outputs", []) or []
        ev_duration = _coerce_seconds(getattr(ev, "duration", None))
        for i, call in enumerate(calls):
            output = outputs[i] if i < len(outputs) else None
            tool_duration = (
                _coerce_seconds(getattr(call, "duration", None))
                or _coerce_seconds(getattr(output, "duration", None))
                or ev_duration
            )
            _emit_tool_span(
                call_sid=call_sid,
                agent_name=agent_name,
                call=call,
                output=output,
                duration_seconds=tool_duration,
            )

    @session.on("session_usage_updated")
    def _on_usage(ev: Any) -> None:
        nonlocal latest_usage_attrs
        collector.record_usage(ev)
        latest_usage_attrs = _usage_attrs_from_event(ev)
        if latest_usage_attrs:
            _emit_metric_span("session_usage", call_sid, agent_name, latest_usage_attrs)

    @session.on("metrics_collected")
    def _on_metrics(ev: Any) -> None:
        # Realtime models emit RealtimeModelMetrics via this event channel
        # rather than via ChatMessage.metrics. We dispatch by class name to
        # avoid an import-time dependency on the metrics types — keeps the
        # SDK module loadable even if the metrics package shape shifts in
        # a livekit-agents upgrade.
        metrics = getattr(ev, "metrics", None)
        if metrics is None:
            return
        if type(metrics).__name__ == "RealtimeModelMetrics":
            collector.record_realtime_metrics(metrics)
            realtime_attrs: dict[str, Any] = {
                "request_id": getattr(metrics, "request_id", None),
            }
            for field in (
                "ttft",
                "duration",
                "session_duration",
                "input_tokens",
                "output_tokens",
                "total_tokens",
                "tokens_per_second",
            ):
                val = getattr(metrics, field, None)
                if val is not None:
                    realtime_attrs[field] = val
            for details_field in ("input_token_details", "output_token_details"):
                details = getattr(metrics, details_field, None)
                if details is not None:
                    realtime_attrs[details_field] = str(details)

            # Cache for the next assistant_message / tool emission so those
            # rows pick up tokens/model/provider/TTFT in their gen_ai.usage.* fields.
            model = getattr(metrics, "model", None) or getattr(metrics, "label", None)
            provider = getattr(metrics, "provider", None) or "google"
            if model:
                latest_realtime["model"] = str(model)
                realtime_attrs["__primary_model"] = str(model)
            if provider:
                latest_realtime["provider"] = str(provider)
                realtime_attrs["__primary_provider"] = str(provider)
            ttft_val = realtime_attrs.get("ttft")
            ttft_seconds = _coerce_seconds(ttft_val)
            if ttft_seconds is not None:
                latest_realtime["llm_node_ttft"] = ttft_seconds
                latest_realtime["ttft"] = ttft_seconds
            for field in ("input_tokens", "output_tokens", "total_tokens", "duration"):
                if realtime_attrs.get(field) is not None:
                    latest_realtime[field] = realtime_attrs[field]

            # Always emit so the Runs view exposes per-turn token/timing rows
            # for the realtime path, not just verbose-mode operators.
            _emit_metric_span("realtime_metrics", call_sid, agent_name, realtime_attrs)

    @session.on("error")
    def _on_error(ev: Any) -> None:
        collector.record_error(ev)
        err = getattr(ev, "error", ev)
        with _start_live_langsmith_span("session_error") as span:
            span.set_attribute("langsmith.span.kind", "chain")
            span.set_attribute("langsmith.span.tags", "error,session")
            if call_sid:
                span.set_attribute("langsmith.metadata.call_sid", call_sid)
            if agent_name:
                span.set_attribute("langsmith.metadata.agent", agent_name)
            span.set_attribute("error", True)
            err_text = str(err)[:1000]
            span.set_attribute("error.message", err_text)
            span.set_attribute("langsmith.metadata.error", err_text)
            try:
                from opentelemetry.trace import Status, StatusCode

                span.set_status(Status(StatusCode.ERROR, err_text[:300] or "session error"))
                # record_exception generates an exception span event with
                # type, message, and stacktrace attributes — visible to
                # any backend, not just LangSmith.
                if isinstance(err, BaseException):
                    span.record_exception(err)
            except Exception:
                pass

    @session.on("close")
    def _on_close(ev: Any) -> None:
        collector.record_close(ev)
        collector.flush()
        _emit_metric_span(
            "session_close",
            call_sid,
            agent_name,
            {
                "reason": str(getattr(ev, "reason", "unknown")),
                "error": str(getattr(ev, "error", "")) or "",
                "duration_seconds": round(time.time() - collector.started_at, 1),
                "total_turns": len(collector.turns),
                "false_interruptions": collector.false_interruptions,
                "tool_calls_total": len(collector.tool_calls),
                **latest_usage_attrs,
            },
        )

    trace(f"telemetry capture wired for call_sid={call_sid}")
    logger.info("telemetry capture wired for call_sid=%s", call_sid)
    return collector
