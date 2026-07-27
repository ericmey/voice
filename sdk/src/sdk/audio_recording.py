"""Full-call audio recording via LiveKit Egress.

Records each call to disk through the ``livekit-egress`` service and
exposes the resulting file path/URL as an OTel span attribute so any
backend (Grafana/Tempo, Phoenix, Datadog, ...) can link the call run to its
recording without a vendor-specific upload SDK.

Enable with ``VOICE_RECORD_AUDIO=true``.

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
* If you set ``VOICE_AUDIO_PUBLIC_BASE_URL`` (e.g.
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
import stat
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .trace import trace

logger = logging.getLogger("voice.agent")


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
    track_recordings: dict[str, AudioTrackRecording] = field(default_factory=dict)
    start_errors: dict[str, str] = field(default_factory=dict)
    pending_starts: set[asyncio.Task[Any]] = field(
        default_factory=set,
        repr=False,
        compare=False,
    )
    starting_perspectives: set[str] = field(default_factory=set, repr=False, compare=False)


@dataclass
class AudioTrackRecording:
    """One isolated LiveKit track captured beside the room composite."""

    perspective: str
    track_sid: str
    egress_id: str | None
    host_path: Path
    container_path: str
    mime_type: str


def _enabled() -> bool:
    """Audio recording is opt-in via ``VOICE_RECORD_AUDIO=true``."""
    return os.environ.get("VOICE_RECORD_AUDIO", "").lower() in ("true", "1", "yes")


def _voice_logs_dir() -> Path:
    return Path(os.environ.get("LIVEKIT_VOICE_LOGS", "logs/voice"))


def _recordings_host_dir() -> Path:
    return Path(
        os.environ.get("LIVEKIT_EGRESS_HOST_RECORDINGS_DIR", str(_voice_logs_dir() / "recordings"))
    )


def _recordings_container_dir() -> str:
    return os.environ.get("LIVEKIT_EGRESS_CONTAINER_RECORDINGS_DIR", "/recordings")


def _prepare_agent_recording_dir(agent_name: str) -> Path:
    """Create the shared host directory with the egress writer contract.

    Agent workers run as root and the pinned LiveKit egress image writes as
    uid=1001,gid=0. A plain ``mkdir`` therefore creates ``0755 root:root``:
    readable by egress but not writable. Setgid + group-write keeps subsequent
    files in the shared group and makes the bind mount actually recordable.
    """
    host_dir = _recordings_host_dir() / agent_name
    host_dir.mkdir(parents=True, exist_ok=True)
    host_dir.chmod(0o2775)
    mode = stat.S_IMODE(host_dir.stat().st_mode)
    if mode != 0o2775:
        raise RuntimeError(
            f"recording directory {host_dir} has mode {mode:o}, expected 2775"
        )
    return host_dir


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


def _perspective_paths(
    recording: CallAudioRecording,
    perspective: str,
) -> tuple[Path, str]:
    extension = _recording_extension()
    filename = f"{recording.call_sid}.{perspective}.{extension}"
    return (
        recording.host_path.parent / filename,
        f"{_recordings_container_dir().rstrip('/')}/{recording.agent_name}/{filename}",
    )


def _public_audio_url(recording: CallAudioRecording) -> str | None:
    """Build a click-through URL when an operator has stood up a static
    file server in front of the recordings dir. Without one, span
    attributes carry the raw filesystem path instead.
    """
    base = os.environ.get("VOICE_AUDIO_PUBLIC_BASE_URL", "").rstrip("/")
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

    host_dir = _prepare_agent_recording_dir(agent_name)
    extension = _recording_extension()
    filename = f"{call_sid}.{extension}"
    host_path = host_dir / filename
    container_path = f"{_recordings_container_dir().rstrip('/')}/{agent_name}/{filename}"

    recording = CallAudioRecording(
        call_sid=call_sid,
        agent_name=agent_name,
        room_name=ctx.room.name,
        egress_id=None,
        host_path=host_path,
        container_path=container_path,
        mime_type=_mime_type(host_path),
        started_at=time.time(),
    )

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
        recording.start_errors["composite"] = f"{type(exc).__name__}: {exc}"
        return recording

    egress_id = getattr(res, "egress_id", None)
    recording.egress_id = egress_id
    logger.info("audio egress started: call_sid=%s egress_id=%s", call_sid, egress_id)
    trace(f"audio egress started call_sid={call_sid} egress_id={egress_id}")
    return recording


def _publication_audio_track_sid(publication: Any) -> str | None:
    """Return an audio publication SID without trusting a name convention."""
    kind = getattr(publication, "kind", None)
    try:
        from livekit import rtc

        if kind != rtc.TrackKind.KIND_AUDIO and str(kind).lower() not in {
            "audio",
            "kind_audio",
        }:
            return None
    except (ImportError, AttributeError):
        # Test doubles and older SDKs may expose only the string kind.
        if str(kind).lower() not in {
            "audio",
            "kind_audio",
        }:
            return None
    sid = getattr(publication, "sid", None)
    return str(sid) if sid else None


def _find_sip_microphone_track_sid(room: Any) -> str | None:
    """Find the caller's published audio track, never an egress/agent track."""
    participants = getattr(room, "remote_participants", {})
    values = participants.values() if hasattr(participants, "values") else participants
    for participant in values:
        identity = str(getattr(participant, "identity", ""))
        if not identity.startswith("sip_"):
            continue
        publications = getattr(participant, "track_publications", {})
        pubs = publications.values() if hasattr(publications, "values") else publications
        for publication in pubs:
            sid = _publication_audio_track_sid(publication)
            if sid:
                return sid
    return None


def _find_local_audio_track_sid(room: Any) -> str | None:
    participant = getattr(room, "local_participant", None)
    publications = getattr(participant, "track_publications", {}) if participant else {}
    pubs = publications.values() if hasattr(publications, "values") else publications
    for publication in pubs:
        sid = _publication_audio_track_sid(publication)
        if sid:
            return sid
    return None


async def _start_track_egress(
    recording: CallAudioRecording,
    *,
    perspective: str,
    track_sid: str,
) -> None:
    """Start one isolated-track recorder without invalidating other evidence."""
    if perspective in recording.track_recordings or perspective in recording.starting_perspectives:
        return
    recording.starting_perspectives.add(perspective)
    host_path, container_path = _perspective_paths(recording, perspective)
    try:
        from livekit import api

        req = api.TrackEgressRequest(
            room_name=recording.room_name,
            track_id=track_sid,
            # Track egress accepts DirectFileOutput; unlike composite egress it
            # does not accept EncodedFileOutput or a file_type selector.
            file=api.DirectFileOutput(filepath=container_path),
        )
        lkapi = api.LiveKitAPI()
        try:
            res = await lkapi.egress.start_track_egress(req)
        finally:
            await lkapi.aclose()
        item = AudioTrackRecording(
            perspective=perspective,
            track_sid=track_sid,
            egress_id=getattr(res, "egress_id", None),
            host_path=host_path,
            container_path=container_path,
            mime_type=_mime_type(host_path),
        )
        recording.track_recordings[perspective] = item
        logger.info(
            "audio %s egress started: call_sid=%s track_sid=%s egress_id=%s",
            perspective,
            recording.call_sid,
            track_sid,
            item.egress_id,
        )
        trace(
            f"audio {perspective} egress started call_sid={recording.call_sid} "
            f"track_sid={track_sid} egress_id={item.egress_id}"
        )
    except Exception as exc:
        recording.start_errors[perspective] = f"{type(exc).__name__}: {exc}"
        logger.warning(
            "audio %s egress start failed for call_sid=%s track_sid=%s: %s",
            perspective,
            recording.call_sid,
            track_sid,
            exc,
        )
        trace(
            f"audio {perspective} egress start failed call_sid={recording.call_sid}: {exc}"
        )
    finally:
        recording.starting_perspectives.discard(perspective)


def _schedule_track_start(
    recording: CallAudioRecording,
    *,
    perspective: str,
    track_sid: str | None,
) -> None:
    if not track_sid:
        return
    if perspective in recording.track_recordings or perspective in recording.starting_perspectives:
        return
    task = asyncio.create_task(
        _start_track_egress(recording, perspective=perspective, track_sid=track_sid)
    )
    recording.pending_starts.add(task)
    task.add_done_callback(recording.pending_starts.discard)


async def _stop_egress(recording: CallAudioRecording | AudioTrackRecording) -> None:
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
    span.set_attribute("voice.audio.call_sid", recording.call_sid)
    span.set_attribute("voice.audio.path", str(recording.host_path))
    span.set_attribute("voice.audio.mime_type", recording.mime_type)
    if recording.egress_id:
        span.set_attribute("voice.audio.egress_id", recording.egress_id)
    for perspective, item in sorted(recording.track_recordings.items()):
        span.set_attribute(f"voice.audio.{perspective}.path", str(item.host_path))
        span.set_attribute(f"voice.audio.{perspective}.track_sid", item.track_sid)
        if item.egress_id:
            span.set_attribute(f"voice.audio.{perspective}.egress_id", item.egress_id)
    for perspective, error in sorted(recording.start_errors.items()):
        span.set_attribute(f"voice.audio.{perspective}.start_error", error)
    public_url = _public_audio_url(recording)
    if public_url:
        span.set_attribute("voice.audio.url", public_url)


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

    The recording stays on
    disk; observability backends discover it via the path / URL span
    attributes written earlier by :func:`annotate_call_audio_recording`.
    """
    if recording is None:
        return
    if recording.pending_starts:
        await asyncio.gather(*tuple(recording.pending_starts), return_exceptions=True)
    for perspective in ("mic", "tts"):
        if (
            perspective not in recording.track_recordings
            and perspective not in recording.start_errors
        ):
            recording.start_errors[perspective] = "track_not_found"
            logger.warning(
                "audio %s egress never started for call_sid=%s: track not found",
                perspective,
                recording.call_sid,
            )

    # LiveKit gives a job process a short shutdown window. Each Egress API call
    # can take several seconds even when the room already completed the egress;
    # serial stops consumed the entire window once three perspectives existed.
    # Stop all independent views together so recording never makes teardown red.
    await asyncio.gather(
        _stop_egress(recording),
        *(
            _stop_egress(item)
            for item in tuple(recording.track_recordings.values())
        ),
    )

    candidates: list[tuple[str, Path]] = []
    if recording.egress_id:
        candidates.append(("composite", recording.host_path))
    candidates.extend(
        (perspective, item.host_path)
        for perspective, item in sorted(recording.track_recordings.items())
        if item.egress_id
    )
    async def _settle(perspective: str, path: Path) -> tuple[str, bool]:
        ready = await _wait_for_recording(path, timeout_seconds)
        if not ready:
            logger.warning(
                "audio %s recording finalize: file not ready: %s", perspective, path
            )
            trace(f"audio {perspective} recording finalize: file not ready: {path}")
            return perspective, False
        logger.info(
            "audio %s recording finalized: %s (%d bytes)",
            perspective,
            path,
            path.stat().st_size,
        )
        trace(f"audio {perspective} recording finalized: {path}")
        return perspective, True

    settled = await asyncio.gather(
        *(_settle(perspective, path) for perspective, path in candidates)
    )
    finalized = [perspective for perspective, ready in settled if ready]
    missing = [perspective for perspective, ready in settled if not ready]

    logger.info(
        "audio recording perspectives: call_sid=%s finalized=%s missing=%s start_errors=%s",
        recording.call_sid,
        finalized,
        missing,
        sorted(recording.start_errors),
    )


def wire_call_audio_attachment(ctx: Any, recording: CallAudioRecording | None) -> None:
    """Start isolated track taps and register one evidence-preserving finalizer."""
    if recording is None:
        return

    room = getattr(ctx, "room", None)
    if room is not None:
        _schedule_track_start(
            recording,
            perspective="mic",
            track_sid=_find_sip_microphone_track_sid(room),
        )
        _schedule_track_start(
            recording,
            perspective="tts",
            track_sid=_find_local_audio_track_sid(room),
        )

        on = getattr(room, "on", None)
        if on is not None:

            @on("local_track_published")
            def _on_local_track_published(publication: Any, _track: Any) -> None:
                _schedule_track_start(
                    recording,
                    perspective="tts",
                    track_sid=_publication_audio_track_sid(publication),
                )

            @on("track_published")
            def _on_remote_track_published(publication: Any, participant: Any) -> None:
                if not str(getattr(participant, "identity", "")).startswith("sip_"):
                    return
                _schedule_track_start(
                    recording,
                    perspective="mic",
                    track_sid=_publication_audio_track_sid(publication),
                )

    add_shutdown_callback = getattr(ctx, "add_shutdown_callback", None)
    if add_shutdown_callback is None:
        return

    async def _finalize_audio() -> None:
        await finalize_call_audio_recording(recording)

    add_shutdown_callback(_finalize_audio)
