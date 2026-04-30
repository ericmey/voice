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
from pathlib import Path
from typing import Any

from livekit.agents import AgentSession
from opentelemetry import trace as otel_trace

from .trace import trace

logger = logging.getLogger("openclaw-livekit.agent")

# OTel tracer for emitting live metric spans alongside the JSON
# telemetry capture. Each session.on(...) hook below uses it to push
# the same data into the OTel pipeline so it lands in LangSmith
# turn-by-turn (within ~1-2s of the event firing) instead of only
# arriving as a postcall summary.
#
# When LANGSMITH_TRACING=false this tracer is a no-op (OTel SDK
# returns a NoOp tracer when no provider is configured), so the
# instrumentation costs nothing in dev / CI / unit tests.
_tracer = otel_trace.get_tracer("openclaw-livekit.metrics")


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
    with _tracer.start_as_current_span(name) as span:
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
            if value is None or value == "":
                continue
            if isinstance(value, bool):
                span.set_attribute(f"langsmith.metadata.{key}", str(value))
            elif isinstance(value, (str, int, float)):
                span.set_attribute(f"langsmith.metadata.{key}", value)


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
        for field in ("ttft", "duration", "input_tokens", "output_tokens", "total_tokens"):
            val = getattr(ev, field, None)
            if val is not None:
                entry[field] = round(val, 4) if isinstance(val, float) else val
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
            self.tool_calls.append(
                {
                    "timestamp": time.time() - self.started_at,
                    "name": name,
                    "success": output is not None,
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
            entry: dict[str, Any] = {
                "type": type(mu).__name__,
                "provider": getattr(mu, "provider", None),
                "model": getattr(mu, "model", None),
            }
            # Token fields (LLM)
            for field in ("input_tokens", "output_tokens"):
                val = getattr(mu, field, None)
                if val is not None:
                    entry[field] = val
            # Audio duration (STT/TTS)
            for field in ("audio_duration", "characters_count"):
                val = getattr(mu, field, None)
                if val is not None:
                    entry[field] = round(val, 2) if isinstance(val, float) else val
            snapshot["models"].append(entry)
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

    Call this AFTER session.start() and BEFORE generate_reply().
    Returns the collector so callers can access it if needed.
    """
    if not call_sid:
        return None

    collector = TelemetryCollector(call_sid, agent_name)

    @session.on("conversation_item_added")
    def _on_item(ev: Any) -> None:
        item = getattr(ev, "item", None)
        if item is None:
            return
        role = getattr(item, "role", None) or "unknown"
        metrics = getattr(item, "metrics", None)
        if not metrics or role != "assistant":
            return

        text = ""
        if hasattr(item, "text_content"):
            text = item.text_content or ""
        elif hasattr(item, "content"):
            content = item.content
            if isinstance(content, str):
                text = content
            elif isinstance(content, list):
                text = " ".join(c for c in content if isinstance(c, str))

        collector.record_turn(metrics, role, text)

        # Live-mirror to OTel. ChatMessage.metrics is a dict on chained
        # pipelines (Party). Realtime models (Nyla / Aoi) emit metrics
        # via the metrics_collected channel instead — see _on_metrics.
        turn_attrs: dict[str, Any] = {
            "turn_index": len(collector.turns) - 1,
            "role": role,
            "text_preview": text[:200] if text else "",
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
        _emit_metric_span(
            "user_state_changed", call_sid, agent_name,
            {"old_state": old, "new_state": new},
        )

    @session.on("agent_state_changed")
    def _on_agent_state(ev: Any) -> None:
        old = str(getattr(ev, "old_state", "?"))
        new = str(getattr(ev, "new_state", "?"))
        collector.record_agent_state(old, new)
        _emit_metric_span(
            "agent_state_changed", call_sid, agent_name,
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
        _emit_metric_span("overlapping_speech", call_sid, agent_name, overlap_attrs)

    @session.on("agent_false_interruption")
    def _on_false_interrupt(ev: Any) -> None:
        collector.false_interruptions += 1
        _emit_metric_span(
            "false_interruption", call_sid, agent_name,
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
        for i, call in enumerate(calls):
            name = str(getattr(call, "name", "unknown"))
            output = outputs[i] if i < len(outputs) else None
            _emit_metric_span(
                "tool_executed", call_sid, agent_name,
                {
                    "tool_name": name,
                    "success": output is not None,
                    "output_preview": str(output)[:300] if output is not None else "",
                },
            )

    @session.on("session_usage_updated")
    def _on_usage(ev: Any) -> None:
        collector.record_usage(ev)
        # Cumulative usage — one OTel span per update. Each model in
        # the usage rollup gets its fields flattened with a model:
        # prefix so e.g. `metadata.gemini-2.5.input_tokens` is filterable.
        usage = getattr(ev, "usage", None)
        if usage is None:
            return
        model_usage = getattr(usage, "model_usage", []) or []
        usage_attrs: dict[str, Any] = {}
        for mu in model_usage:
            mtype = type(mu).__name__
            model = getattr(mu, "model", None) or "unknown"
            provider = getattr(mu, "provider", None) or "unknown"
            prefix = f"{mtype.lower()}.{model}"
            usage_attrs[f"{prefix}.provider"] = provider
            for field in ("input_tokens", "output_tokens", "audio_duration", "characters_count"):
                val = getattr(mu, field, None)
                if val is not None:
                    usage_attrs[f"{prefix}.{field}"] = val
        if usage_attrs:
            _emit_metric_span("session_usage", call_sid, agent_name, usage_attrs)

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
            # Live-mirror: this is THE per-turn signal for Nyla / Aoi.
            # Without forwarding to OTel here, realtime calls show up
            # in LangSmith with no latency or token data.
            realtime_attrs: dict[str, Any] = {
                "request_id": getattr(metrics, "request_id", None),
            }
            for field in ("ttft", "duration", "input_tokens", "output_tokens", "total_tokens"):
                val = getattr(metrics, field, None)
                if val is not None:
                    realtime_attrs[field] = val
            _emit_metric_span("realtime_metrics", call_sid, agent_name, realtime_attrs)

    @session.on("error")
    def _on_error(ev: Any) -> None:
        collector.record_error(ev)
        _emit_metric_span(
            "session_error", call_sid, agent_name,
            {"error": str(getattr(ev, "error", ev))[:500]},
        )

    @session.on("close")
    def _on_close(ev: Any) -> None:
        collector.record_close(ev)
        collector.flush()
        _emit_metric_span(
            "session_close", call_sid, agent_name,
            {
                "reason": str(getattr(ev, "reason", "unknown")),
                "error": str(getattr(ev, "error", "")) or "",
                "duration_seconds": round(time.time() - collector.started_at, 1),
                "total_turns": len(collector.turns),
                "false_interruptions": collector.false_interruptions,
                "tool_calls_total": len(collector.tool_calls),
            },
        )

    trace(f"telemetry capture wired for call_sid={call_sid}")
    logger.info("telemetry capture wired for call_sid=%s", call_sid)
    return collector
