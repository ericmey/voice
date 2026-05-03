"""Per-call telemetry capture — structured JSON for post-call review.

Hooks into :class:`AgentSession` events to capture:

- Per-turn latency (e2e, LLM TTFT, TTS TTFB, transcription delay)
- User / agent state transitions with timestamps
- Overlapping speech and interruption data
- Tool execution timing
- Token usage breakdown
- Realtime-model metrics (Gemini realtime, etc.)
- Session-level summary stats

Output: a single JSON file at
``$LIVEKIT_VOICE_LOGS/call-telemetry/{call_sid}.json`` written on
session close. If ``LIVEKIT_VOICE_LOGS`` is unset, capture is a no-op.

This file is the primary input to ``postcall.py`` (post-call review). It
is written off the OTel pipeline by design — OTel spans cover the
SigNoz trace tree, this JSON covers the analyst's per-call ground truth.
LiveKit Agents emits all the OTel spans / attributes the SigNoz LiveKit
dashboard needs natively; we add no enrichment.
"""

from __future__ import annotations

import json
import logging
import os
import time
from pathlib import Path
from typing import Any

from livekit.agents import AgentSession

from .trace import trace

logger = logging.getLogger("openclaw-livekit.agent")

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


def _telemetry_dir() -> Path | None:
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

        self.user_states: list[dict[str, Any]] = []
        self.agent_states: list[dict[str, Any]] = []

        self.overlapping_speech: list[dict[str, Any]] = []
        self.false_interruptions: int = 0

        self.tool_calls: list[dict[str, Any]] = []
        self.usage_snapshots: list[dict[str, Any]] = []
        self.errors: list[dict[str, Any]] = []

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
        - ``input_tokens`` / ``output_tokens``: usage breakdown.
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
            {"timestamp": time.time() - self.started_at, "old": old, "new": new}
        )

    def record_agent_state(self, old: str, new: str) -> None:
        self.agent_states.append(
            {"timestamp": time.time() - self.started_at, "old": old, "new": new}
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
        snapshot: dict[str, Any] = {"timestamp": time.time() - self.started_at, "models": []}
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

        # Latency stats — chained pipeline emits true per-turn e2e metrics
        # rolled into ChatMessage.metrics; realtime models emit TTFT via a
        # separate ``metrics_collected`` event. Keep those signals distinct:
        # realtime TTFT is useful, but it is not end-to-end response latency.
        e2e_values = [t["e2e_latency"] for t in self.turns if "e2e_latency" in t]
        ttft_values = [t["llm_node_ttft"] for t in self.turns if "llm_node_ttft" in t]
        realtime_ttft_values = [m["ttft"] for m in self.realtime_metrics if m.get("ttft", -1) >= 0]
        if not ttft_values and self.realtime_metrics:
            ttft_values = realtime_ttft_values

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
            "realtime_ttft": _stats(realtime_ttft_values),
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

    Call this BEFORE ``session.start()`` so startup state, greeting,
    usage, and later turns all land in the collector. Returns the
    collector so callers can access it if needed.
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
        if metrics and role == "assistant":
            collector.record_turn(metrics, role, clean_text)

    @session.on("user_state_changed")
    def _on_user_state(ev: Any) -> None:
        old = str(getattr(ev, "old_state", "?"))
        new = str(getattr(ev, "new_state", "?"))
        collector.record_user_state(old, new)

    @session.on("agent_state_changed")
    def _on_agent_state(ev: Any) -> None:
        old = str(getattr(ev, "old_state", "?"))
        new = str(getattr(ev, "new_state", "?"))
        collector.record_agent_state(old, new)

    @session.on("overlapping_speech")
    def _on_overlap(ev: Any) -> None:
        collector.record_overlap(ev)

    @session.on("agent_false_interruption")
    def _on_false_interrupt(_ev: Any) -> None:
        collector.false_interruptions += 1

    @session.on("function_tools_executed")
    def _on_tools(ev: Any) -> None:
        collector.record_tool_execution(ev)

    @session.on("session_usage_updated")
    def _on_usage(ev: Any) -> None:
        collector.record_usage(ev)

    @session.on("metrics_collected")
    def _on_metrics(ev: Any) -> None:
        # Realtime models emit RealtimeModelMetrics via this channel
        # rather than via ChatMessage.metrics. Dispatch by class name to
        # avoid an import-time dependency on the metrics types — keeps
        # the SDK module loadable even if the metrics package shape
        # shifts in a livekit-agents upgrade.
        metrics = getattr(ev, "metrics", None)
        if metrics is None:
            return
        if type(metrics).__name__ == "RealtimeModelMetrics":
            collector.record_realtime_metrics(metrics)

    @session.on("error")
    def _on_error(ev: Any) -> None:
        collector.record_error(ev)

    @session.on("close")
    def _on_close(ev: Any) -> None:
        collector.record_close(ev)
        collector.flush()

    trace(f"telemetry capture wired for call_sid={call_sid}")
    logger.info("telemetry capture wired for call_sid=%s", call_sid)
    return collector


__all__ = [
    "TelemetryCollector",
    "wire_telemetry_capture",
]
