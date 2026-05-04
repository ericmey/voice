"""Full-call audio recording via LiveKit Egress.

Records each call to disk through the ``livekit-egress`` service and
exposes the resulting file path/URL as an OTel span attribute so any
backend (Grafana/Tempo, Phoenix, Datadog, ...) can link the call run to its
recording without a vendor-specific upload SDK.

Enable with ``OPENCLAW_RECORD_AUDIO=true``.

The agent entrypoint calls :func:`start_call_audio_recording` after
``ctx.connect``, :func:`annotate_call_audio_recording` immediately after
``session.start()`` (while LiveKit's ``agent_session`` span is active),
and :func:`wire_call_audio_attachment` to register a shutdown hook that
finalizes the egress file.

Where to listen to the audio:

* ``LIVEKIT_EGRESS_HOST_RECORDINGS_DIR`` (default
  ``logs/voice/recordings``) is the host-mounted directory egress
  writes to. Files appear at
  ``<host_dir>/<agent>/<call_sid>.<ext>``.
* If you set ``OPENCLAW_AUDIO_PUBLIC_BASE_URL`` (e.g.
  ``http://my-host:8090/recordings``), the span attribute becomes a
  clickable URL instead of a file path. Stand up a tiny static-file
  server pointed at the host recordings dir to enable click-to-listen
  from any observability UI.
"""

from __future__ import annotations

import asyncio
import logging
import mimetypes
import os
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .trace import trace

logger = logging.getLogger("openclaw-livekit.agent")


@dataclass
class CallAudioRecording:
    call_sid: str
    agent_name: str
    room_name: str
    egress_id: str | None
    host_path: Path
    container_path: str
    mime_type: str
    started_at: float


def _enabled() -> bool:
    """Audio recording is opt-in via ``OPENCLAW_RECORD_AUDIO=true``."""
    return os.environ.get("OPENCLAW_RECORD_AUDIO", "").lower() in ("true", "1", "yes")


def _voice_logs_dir() -> Path:
    return Path(os.environ.get("LIVEKIT_VOICE_LOGS", "logs/voice"))


def _recordings_host_dir() -> Path:
    return Path(
        os.environ.get("LIVEKIT_EGRESS_HOST_RECORDINGS_DIR", str(_voice_logs_dir() / "recordings"))
    )


def _recordings_container_dir() -> str:
    return os.environ.get("LIVEKIT_EGRESS_CONTAINER_RECORDINGS_DIR", "/recordings")


def _recording_extension() -> str:
    return os.environ.get("LIVEKIT_EGRESS_AUDIO_EXTENSION", "ogg").lstrip(".") or "ogg"


def _recording_file_type(extension: str) -> Any:
    from livekit import api

    return {
        "ogg": api.EncodedFileType.OGG,
        "mp4": api.EncodedFileType.MP4,
    }.get(extension.lower(), api.EncodedFileType.OGG)


def _mime_type(path: Path) -> str:
    return mimetypes.guess_type(path.name)[0] or "audio/ogg"


def _public_audio_url(recording: CallAudioRecording) -> str | None:
    """Build a click-through URL when an operator has stood up a static
    file server in front of the recordings dir. Without one, span
    attributes carry the raw filesystem path instead.
    """
    base = os.environ.get("OPENCLAW_AUDIO_PUBLIC_BASE_URL", "").rstrip("/")
    if not base:
        return None
    return f"{base}/{recording.agent_name}/{recording.host_path.name}"


async def start_call_audio_recording(
    ctx: Any,
    *,
    call_sid: str | None,
    agent_name: str,
) -> CallAudioRecording | None:
    """Start audio-only room composite egress for the current call."""
    if not _enabled() or not call_sid:
        return None

    host_dir = _recordings_host_dir() / agent_name
    host_dir.mkdir(parents=True, exist_ok=True)
    extension = _recording_extension()
    filename = f"{call_sid}.{extension}"
    host_path = host_dir / filename
    container_path = f"{_recordings_container_dir().rstrip('/')}/{agent_name}/{filename}"

    try:
        from livekit import api

        req = api.RoomCompositeEgressRequest(
            room_name=ctx.room.name,
            audio_only=True,
            file_outputs=[
                api.EncodedFileOutput(
                    file_type=_recording_file_type(extension),
                    filepath=container_path,
                )
            ],
        )
        lkapi = api.LiveKitAPI()
        try:
            res = await lkapi.egress.start_room_composite_egress(req)
        finally:
            await lkapi.aclose()
    except Exception as exc:
        logger.warning("audio egress start failed for call_sid=%s: %s", call_sid, exc)
        trace(f"audio egress start failed call_sid={call_sid}: {exc}")
        return None

    egress_id = getattr(res, "egress_id", None)
    recording = CallAudioRecording(
        call_sid=call_sid,
        agent_name=agent_name,
        room_name=ctx.room.name,
        egress_id=egress_id,
        host_path=host_path,
        container_path=container_path,
        mime_type=_mime_type(host_path),
        started_at=time.time(),
    )
    logger.info("audio egress started: call_sid=%s egress_id=%s", call_sid, egress_id)
    trace(f"audio egress started call_sid={call_sid} egress_id={egress_id}")
    return recording


async def _stop_egress(recording: CallAudioRecording) -> None:
    if not recording.egress_id:
        return
    try:
        from livekit import api

        lkapi = api.LiveKitAPI()
        try:
            await lkapi.egress.stop_egress(api.StopEgressRequest(egress_id=recording.egress_id))
        finally:
            await lkapi.aclose()
    except Exception as exc:
        logger.info("audio egress stop skipped/failed for %s: %s", recording.egress_id, exc)


async def _wait_for_recording(path: Path, timeout_seconds: float) -> bool:
    deadline = time.monotonic() + timeout_seconds
    last_size = -1
    stable_since: float | None = None
    while time.monotonic() < deadline:
        if path.exists():
            size = path.stat().st_size
            if size > 0 and size == last_size:
                stable_since = stable_since or time.monotonic()
                if time.monotonic() - stable_since >= 1.0:
                    return True
            else:
                stable_since = None
            last_size = size
        await asyncio.sleep(0.5)
    return False


def _annotate_active_span(recording: CallAudioRecording) -> None:
    """Decorate the currently active OTel span with audio recording metadata.

    This must run while LiveKit's ``agent_session`` span is active. The
    job shutdown callback runs after LiveKit closes the session span, so
    finalization is for disk correctness, not span mutation.
    """
    try:
        from opentelemetry import trace as otel_trace
    except ImportError:
        return
    span = otel_trace.get_current_span()
    if span is None or not span.is_recording():
        return
    span.set_attribute("openclaw.audio.call_sid", recording.call_sid)
    span.set_attribute("openclaw.audio.path", str(recording.host_path))
    span.set_attribute("openclaw.audio.mime_type", recording.mime_type)
    if recording.egress_id:
        span.set_attribute("openclaw.audio.egress_id", recording.egress_id)
    public_url = _public_audio_url(recording)
    if public_url:
        span.set_attribute("openclaw.audio.url", public_url)


def annotate_call_audio_recording(recording: CallAudioRecording | None) -> None:
    """Stamp recording path/URL metadata on the active ``agent_session`` span."""
    if recording is None:
        return
    _annotate_active_span(recording)


async def finalize_call_audio_recording(
    recording: CallAudioRecording | None,
    *,
    timeout_seconds: float = 30.0,
) -> None:
    """Stop egress and wait for the file to settle.

    Replaces the legacy LangSmith upload path. The recording stays on
    disk; observability backends discover it via the path / URL span
    attributes written earlier by :func:`annotate_call_audio_recording`.
    """
    if recording is None:
        return
    await _stop_egress(recording)
    ready = await _wait_for_recording(recording.host_path, timeout_seconds)
    if not ready:
        logger.warning("audio recording finalize: file not ready: %s", recording.host_path)
        trace(f"audio recording finalize: file not ready: {recording.host_path}")
        return
    logger.info(
        "audio recording finalized: %s (%d bytes)",
        recording.host_path,
        recording.host_path.stat().st_size,
    )
    trace(f"audio recording finalized: {recording.host_path}")


# Backwards-compatible alias — agents currently call this name.
attach_call_audio_to_langsmith = finalize_call_audio_recording


def wire_call_audio_attachment(ctx: Any, recording: CallAudioRecording | None) -> None:
    """Register a shutdown hook that finalizes the recording file."""
    if recording is None:
        return
    add_shutdown_callback = getattr(ctx, "add_shutdown_callback", None)
    if add_shutdown_callback is None:
        return

    async def _finalize_audio() -> None:
        await finalize_call_audio_recording(recording)

    add_shutdown_callback(_finalize_audio)
