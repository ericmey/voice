"""Post-call review system — per-call manifest tracking.

Every call that closes gets logged in the manifest. Automated QC review
used to spawn a Rin agent through an external CLI gateway; that gateway is
retired, so calls are recorded as ``no_reviewer`` until a replacement
reviewer is wired in.

All file paths resolve from ``$LIVEKIT_VOICE_LOGS``. If that env var is
unset, post-call review is a no-op.
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

logger = logging.getLogger("voice.agent")


def _voice_logs() -> Path | None:
    logs = os.environ.get("LIVEKIT_VOICE_LOGS")
    return Path(logs) if logs else None


def _transcript_path(call_sid: str) -> Path | None:
    base = _voice_logs()
    return base / "phone-transcripts" / f"{call_sid}.txt" if base else None


def _manifest_path() -> Path | None:
    base = _voice_logs()
    return base / "call-manifest.jsonl" if base else None


# --- manifest -----------------------------------------------------------


def _append_manifest(entry: dict) -> None:
    """Append one JSON line to the call manifest."""
    path = _manifest_path()
    if path is None:
        return
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as f:
            f.write(json.dumps(entry, separators=(",", ":")) + "\n")
    except Exception as err:
        logger.error("postcall: manifest write failed: %s", err)


# --- wiring -------------------------------------------------------------


def wire_postcall_review(
    session: AgentSession,
    call_sid: str | None,
    agent_name: str = "unknown",
) -> None:
    """Register a ``close`` handler that logs the call and spawns Rin.

    Call this AFTER ``wire_transcript_logging`` in the agent entrypoint.
    """
    if not call_sid:
        return

    @session.on("close")
    def _on_close(ev: Any) -> None:
        error = getattr(ev, "error", None)
        reason = str(getattr(ev, "reason", "unknown"))
        ended_at = time.strftime("%Y-%m-%dT%H:%M:%S%z")

        transcript_path = _transcript_path(call_sid)
        has_transcript = transcript_path.exists() if transcript_path else False

        # Always log to manifest — even if no transcript
        manifest_entry = {
            "call_sid": call_sid,
            "agent": agent_name,
            "ended_at": ended_at,
            "close_reason": reason,
            "has_error": error is not None,
            "error_detail": str(error) if error else None,
            "has_transcript": has_transcript,
            "review_status": "pending",
        }

        if not has_transcript:
            manifest_entry["review_status"] = "skipped_no_transcript"
            _append_manifest(manifest_entry)
            trace(f"postcall: no transcript for {call_sid}, logged as skipped")
            return

        # No reviewer wired. The external CLI gateway that spawned Rin for QC is
        # retired; the manifest still records every closed call so a future
        # reviewer can sweep them.
        manifest_entry["review_status"] = "no_reviewer"
        trace(f"postcall: manifest logged for {call_sid} (no reviewer wired)")

        _append_manifest(manifest_entry)

    trace(f"postcall review wired for call_sid={call_sid}")
