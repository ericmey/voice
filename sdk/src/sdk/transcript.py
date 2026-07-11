"""Per-call transcript logging — file + trace + logger."""

from __future__ import annotations

import logging
import os
import time
from pathlib import Path
from typing import Any

from livekit.agents import AgentSession

from .trace import trace

logger = logging.getLogger("voice.agent")


def _transcript_dir() -> Path | None:
    """Resolve the transcript directory from LIVEKIT_VOICE_LOGS, or None."""
    logs = os.environ.get("LIVEKIT_VOICE_LOGS")
    return Path(logs) / "phone-transcripts" if logs else None


def _ensure_transcript_dir() -> Path | None:
    """Create and return the transcript dir, or None if logging is disabled."""
    d = _transcript_dir()
    if d is None:
        return None
    try:
        d.mkdir(parents=True, exist_ok=True)
    except Exception as exc:
        logger.error("transcript dir creation failed: %s", exc)
        return None
    return d


def _write_transcript_line(call_sid: str | None, role: str, text: str) -> None:
    """Write a transcript line to the per-call file + trace + logger."""
    ts = time.strftime("%H:%M:%S")
    tag = call_sid or "unknown"
    line = f"[{ts}] [{role.upper()}] {text}"

    logger.info("[TRANSCRIPT:%s] %s: %s", tag, role.upper(), text)
    trace(f"[TRANSCRIPT:{tag}] {role.upper()}: {text}")

    if call_sid:
        d = _transcript_dir()
        if d is None:
            return
        try:
            path = d / f"{call_sid}.txt"
            with path.open("a", encoding="utf-8") as f:
                f.write(line + "\n")
        except Exception as exc:
            logger.error("transcript line write failed: %s", exc)


def wire_transcript_logging(
    session: AgentSession,
    call_sid: str | None,
    # agent_name is REQUIRED. It used to default to "unknown".
    #
    # Every one of the 12 call sites passes it, so the default never fired — it existed only
    # to SWALLOW a future wiring mistake. And what it would swallow is an identity error: a
    # transcript, a telemetry span, or a call review attributed to nobody, filed under
    # "unknown", silently, forever. Every per-agent dashboard panel and alert selector
    # (`voice-.*`) would match nothing and look healthy.
    #
    # Same lesson as ENV AGENT=aoi, the default persona, and voice="Leda": a default in the
    # identity path is not a convenience. It is a misattribution waiting for the first person
    # who forgets an argument.
    agent_name: str,
) -> None:
    """Register event listeners on *session* that capture transcripts.

    Call this BEFORE ``session.start()`` so the startup greeting and every
    subsequent turn are captured.
    """
    d = _ensure_transcript_dir()

    if call_sid and d is not None:
        try:
            path = d / f"{call_sid}.txt"
            with path.open("a", encoding="utf-8") as f:
                f.write(
                    f"=== Call {call_sid} started at {time.strftime('%Y-%m-%dT%H:%M:%S')} ===\n"
                )
        except Exception as exc:
            logger.error("transcript header write failed: %s", exc)

    @session.on("conversation_item_added")
    def _on_conversation_item(ev: Any) -> None:
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

        if text.strip():
            clean_text = text.strip()
            _write_transcript_line(call_sid, role, clean_text)

    logger.info(
        "transcript logging wired for call_sid=%s (dir=%s)",
        call_sid,
        d,
    )
    trace(f"transcript logging wired call_sid={call_sid}")
