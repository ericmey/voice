"""Decode + resample uploads to the exact shape Riva's streaming ASR expects.

Riva wants 16 kHz mono s16le PCM. Callers send whatever their client produced --
wav at 48k stereo, mp3, m4a, ogg -- so this module normalizes, and it FAILS
LOUDLY rather than handing the recognizer something it will silently
mis-transcribe. A wrong-rate stream does not error; it returns fluent, confident,
wrong text. That is the worst possible outcome, so every conversion here is
explicit and every failure raises.

Two paths, deliberately:
  * fast path -- the upload is already RIFF/WAVE 16 kHz mono s16le, so its
    frames are used as-is with no external process.
  * everything else -- ffmpeg. Not audioop: that module is deprecated in 3.12
    and REMOVED in 3.13, so building on it would put a fixed expiry date on the
    service. Not a hand-rolled resampler either -- correct resampling is real
    signal processing and this is a seam, not a DSP project.
"""

from __future__ import annotations

import io
import os
import shutil
import subprocess
import wave

TARGET_RATE = 16000
TARGET_WIDTH = 2  # s16
TARGET_CHANNELS = 1

FFMPEG_CANDIDATES = ("/opt/homebrew/bin/ffmpeg", "/usr/bin/ffmpeg", "/usr/local/bin/ffmpeg")


class AudioError(ValueError):
    """Input audio could not be normalized. Always surfaced, never swallowed."""


def find_ffmpeg() -> str | None:
    """Resolve ffmpeg to an absolute path.

    A daemon does not inherit a login shell's PATH, so `shutil.which` alone
    works when a human runs this and fails under launchd/systemd. We check the
    known install locations too, and return None rather than guessing.
    """
    found = shutil.which("ffmpeg")
    if found:
        return found
    for candidate in FFMPEG_CANDIDATES:
        if os.path.isfile(candidate) and os.access(candidate, os.X_OK):
            return candidate
    return None


def _already_target(data: bytes) -> bytes | None:
    """Return raw frames if the upload is exactly what Riva wants, else None."""
    if data[:4] != b"RIFF":
        return None
    try:
        with wave.open(io.BytesIO(data), "rb") as w:
            if (
                w.getframerate() == TARGET_RATE
                and w.getnchannels() == TARGET_CHANNELS
                and w.getsampwidth() == TARGET_WIDTH
                and w.getcomptype() == "NONE"
            ):
                return w.readframes(w.getnframes())
    except (wave.Error, EOFError):
        return None  # malformed or exotic wav -- let ffmpeg have a real look
    return None


def _via_ffmpeg(data: bytes) -> bytes:
    exe = find_ffmpeg()
    if exe is None:
        raise AudioError(
            "audio needs conversion but ffmpeg was not found; send 16 kHz mono "
            "16-bit wav, or install ffmpeg in the service image"
        )
    proc = subprocess.run(  # noqa: S603 - fixed argv, absolute exe, bytes on stdin
        [
            exe, "-v", "error", "-nostdin",
            "-i", "pipe:0",
            "-f", "s16le",
            "-acodec", "pcm_s16le",
            "-ac", str(TARGET_CHANNELS),
            "-ar", str(TARGET_RATE),
            "pipe:1",
        ],
        input=data,
        capture_output=True,
        timeout=120,
    )
    if proc.returncode != 0 or not proc.stdout:
        reason = proc.stderr.decode(errors="replace").strip()[:300] or "no output produced"
        raise AudioError(f"could not decode audio: {reason}")
    return proc.stdout


def to_riva_pcm(data: bytes) -> tuple[bytes, float]:
    """Return (pcm_s16le_mono_16k, duration_seconds).

    Raises AudioError on anything that cannot be normalized. Never returns
    best-effort bytes.
    """
    if not data:
        raise AudioError("empty upload")

    pcm = _already_target(data)
    if pcm is None:
        pcm = _via_ffmpeg(data)

    if not pcm:
        raise AudioError("audio normalized to zero samples")
    return pcm, len(pcm) / (TARGET_RATE * TARGET_WIDTH)
