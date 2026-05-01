"""Full-call audio recording and LangSmith attachment upload."""

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
    value = os.environ.get("LANGSMITH_ATTACH_AUDIO")
    if value is not None:
        return value.lower() in ("true", "1", "yes")
    return os.environ.get("LANGSMITH_TRACING", "").lower() in ("true", "1", "yes")


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


def _langsmith_project() -> str | None:
    project = os.environ.get("LANGSMITH_PROJECT")
    if project:
        return project

    headers = os.environ.get("OTEL_EXPORTER_OTLP_HEADERS", "")
    for part in headers.split(","):
        if part.lower().startswith("langsmith-project="):
            return part.split("=", 1)[1]
    return None


def _langsmith_api_key() -> str | None:
    if api_key := os.environ.get("LANGSMITH_API_KEY"):
        return api_key
    headers = os.environ.get("OTEL_EXPORTER_OTLP_HEADERS", "")
    for part in headers.split(","):
        if part.lower().startswith("x-api-key="):
            return part.split("=", 1)[1]
    return None


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


def _upload_langsmith_attachment(recording: CallAudioRecording) -> None:
    from langsmith import Client
    from langsmith.run_trees import RunTree

    project = _langsmith_project()
    api_key = _langsmith_api_key()
    if not project or not api_key:
        logger.warning("LangSmith audio attachment skipped: missing project or API key")
        return

    client = Client(
        api_key=api_key,
        api_url=os.environ.get("LANGSMITH_ENDPOINT", "https://api.smith.langchain.com"),
    )
    metadata = {
        "thread_id": recording.call_sid,
        "call_sid": recording.call_sid,
        "agent": recording.agent_name,
        "room": recording.room_name,
        "egress_id": recording.egress_id,
        "recording_path": str(recording.host_path),
        "recording_mime_type": recording.mime_type,
        "recording_bytes": recording.host_path.stat().st_size,
        "recording_duration_seconds": round(time.time() - recording.started_at, 1),
    }
    run = RunTree(
        name="call_audio",
        run_type="chain",
        project_name=project,
        ls_client=client,
        inputs={"call_sid": recording.call_sid, "room": recording.room_name},
        outputs={"recording_path": str(recording.host_path)},
        tags=["audio", "recording", recording.agent_name],
        extra={"metadata": metadata},
        attachments={"full_call_audio": (recording.mime_type, recording.host_path)},
        dangerously_allow_filesystem=True,
    )
    run.end(outputs={"recording_path": str(recording.host_path)}, metadata=metadata)
    run.post()
    run.wait()


async def attach_call_audio_to_langsmith(
    recording: CallAudioRecording | None,
    *,
    timeout_seconds: float = 30.0,
) -> None:
    """Finalize egress and upload the recorded audio file to LangSmith."""
    if recording is None:
        return
    await _stop_egress(recording)
    ready = await _wait_for_recording(recording.host_path, timeout_seconds)
    if not ready:
        logger.warning("audio attachment skipped; recording not ready: %s", recording.host_path)
        trace(f"audio attachment skipped recording not ready: {recording.host_path}")
        return

    try:
        await asyncio.to_thread(_upload_langsmith_attachment, recording)
    except Exception as exc:
        logger.warning("LangSmith audio attachment upload failed: %s", exc)
        trace(f"LangSmith audio attachment upload failed: {exc}")
        return
    logger.info("LangSmith audio attachment uploaded: %s", recording.host_path)
    trace(f"LangSmith audio attachment uploaded: {recording.host_path}")


def wire_call_audio_attachment(ctx: Any, recording: CallAudioRecording | None) -> None:
    """Attach full-call audio during LiveKit job shutdown."""
    if recording is None:
        return
    add_shutdown_callback = getattr(ctx, "add_shutdown_callback", None)
    if add_shutdown_callback is None:
        return

    async def _upload_audio() -> None:
        await attach_call_audio_to_langsmith(recording)

    add_shutdown_callback(_upload_audio)
