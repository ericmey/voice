#!/usr/bin/env python3
"""Hermes command-TTS adapter for the voicebook service.

Speaks a girl's ACCEPTED voicebook master. The service owns the master path,
the reference transcript and the expected hash — this adapter sends a voice_id
and nothing else, deliberately.

Contract (hermes-agent/tools/tts_tool.py): read text from {input_path}, write
audio to {output_path}, exit 0. Non-zero exit or empty output is surfaced to
the user as an error.

NO FALLBACK. If the service is unreachable this exits non-zero and writes
NOTHING. It must never quietly substitute a different voice — a stock stand-in
that sounds nearly right is worse than silence, because nobody notices.

Importable: functions plus a __main__ guard, so ``atomic_write`` can be tested
directly instead of only through the CLI. An earlier CLI-only test could not
distinguish the atomic path from a naive write at all.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import tempfile
import urllib.error
import urllib.request
import uuid
from pathlib import Path

DEFAULT_URL = "http://10.0.20.25:5055/speak"


def atomic_write(path: Path, payload: bytes) -> None:
    """Write *payload* to *path* atomically, leaving nothing behind on failure.

    Writing straight to the destination means a partial write — disk full,
    interruption, crash mid-stream — leaves a truncated file while the caller
    exits non-zero. Hermes would then see BOTH an error AND an output file.
    Worse, a plain ``open(path, "wb")`` truncates an existing destination
    immediately, destroying the previous good render before the new one is
    known to be complete.

    Temp file in the DESTINATION directory (same filesystem, so ``os.replace``
    is atomic), fsynced, unlinked on every failure path, moved into place only
    once the bytes are durably on disk.
    """
    tmp: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            dir=path.parent, prefix=f".{path.name}.", suffix=".part", delete=False
        ) as fh:
            tmp = Path(fh.name)
            fh.write(payload)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, path)
        tmp = None
    finally:
        if tmp is not None:
            tmp.unlink(missing_ok=True)


def fetch(url: str, voice_id: str, text: str, timeout: float, request_id: str) -> bytes:
    """POST to the service and return WAV bytes. Raises on any failure."""
    req = urllib.request.Request(
        url,
        data=json.dumps({"voice_id": voice_id, "text": text}).encode("utf-8"),
        headers={"Content-Type": "application/json", "X-Request-ID": request_id},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return resp.read()


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--input", required=True)
    p.add_argument("--output", required=True)
    p.add_argument("--voice-id", required=True)
    p.add_argument("--url", default=DEFAULT_URL)
    p.add_argument("--timeout", type=float, default=300.0)
    a = p.parse_args(argv)

    # Correlation id — lets a service log line be matched to this exact
    # invocation without logging any message text.
    request_id = uuid.uuid4().hex[:12]

    try:
        text = open(a.input, encoding="utf-8").read().strip()
    except OSError as exc:
        print(f"voicebook-tts[{request_id}]: cannot read {a.input}: {exc}", file=sys.stderr)
        return 1
    if not text:
        print(f"voicebook-tts[{request_id}]: input text is empty", file=sys.stderr)
        return 1

    try:
        audio = fetch(a.url, a.voice_id, text, a.timeout, request_id)
    except urllib.error.HTTPError as exc:
        body = exc.read()[:300].decode("utf-8", "replace")
        print(f"voicebook-tts[{request_id}]: HTTP {exc.code} from {a.url}: {body}", file=sys.stderr)
        return 1
    except Exception as exc:
        print(f"voicebook-tts[{request_id}]: {a.url} unreachable: {exc}", file=sys.stderr)
        return 1

    if len(audio) < 100 or audio[:4] != b"RIFF":
        print(
            f"voicebook-tts[{request_id}]: response was not WAV ({len(audio)} bytes)",
            file=sys.stderr,
        )
        return 1

    try:
        atomic_write(Path(a.output), audio)
    except OSError as exc:
        print(f"voicebook-tts[{request_id}]: cannot write {a.output}: {exc}", file=sys.stderr)
        return 1

    print(f"voicebook-tts[{request_id}]: {len(audio)} bytes -> {a.output} (voice_id={a.voice_id})")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
