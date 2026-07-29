#!/usr/bin/env python3
"""Hermes command-TTS adapter for the named Magpie voice registry.

Reads text from ``--input`` and atomically writes a completed WAV to
``--output``. The client sends a stable voice ID only; prompt paths, hashes,
quality, and language remain server-owned. There is deliberately no fallback.
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

DEFAULT_URL = "http://10.0.20.25:5056/speak"


def atomic_write(path: Path, payload: bytes) -> None:
    tmp: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            dir=path.parent, prefix=f".{path.name}.", suffix=".part", delete=False
        ) as output:
            tmp = Path(output.name)
            output.write(payload)
            output.flush()
            os.fsync(output.fileno())
        os.replace(tmp, path)
        tmp = None
    finally:
        if tmp is not None:
            tmp.unlink(missing_ok=True)


def fetch(url: str, voice_id: str, text: str, timeout: float, request_id: str) -> bytes:
    request = urllib.request.Request(
        url,
        data=json.dumps({"voice_id": voice_id, "text": text}).encode("utf-8"),
        headers={"Content-Type": "application/json", "X-Request-ID": request_id},
        method="POST",
    )
    with urllib.request.urlopen(request, timeout=timeout) as response:
        return response.read()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--input", required=True)
    parser.add_argument("--output", required=True)
    parser.add_argument("--voice-id", required=True)
    parser.add_argument("--url", default=DEFAULT_URL)
    parser.add_argument("--timeout", type=float, default=300.0)
    args = parser.parse_args(argv)
    request_id = uuid.uuid4().hex[:12]

    try:
        text = Path(args.input).read_text(encoding="utf-8").strip()
    except OSError as exc:
        print(f"magpie-tts[{request_id}]: cannot read {args.input}: {exc}", file=sys.stderr)
        return 1
    if not text:
        print(f"magpie-tts[{request_id}]: input text is empty", file=sys.stderr)
        return 1

    try:
        audio = fetch(args.url, args.voice_id, text, args.timeout, request_id)
    except urllib.error.HTTPError as exc:
        body = exc.read()[:300].decode("utf-8", "replace")
        print(
            f"magpie-tts[{request_id}]: HTTP {exc.code} from {args.url}: {body}",
            file=sys.stderr,
        )
        return 1
    except Exception as exc:
        print(f"magpie-tts[{request_id}]: {args.url} unreachable: {exc}", file=sys.stderr)
        return 1

    if len(audio) < 100 or audio[:4] != b"RIFF":
        print(
            f"magpie-tts[{request_id}]: response was not WAV ({len(audio)} bytes)",
            file=sys.stderr,
        )
        return 1
    try:
        atomic_write(Path(args.output), audio)
    except OSError as exc:
        print(f"magpie-tts[{request_id}]: cannot write {args.output}: {exc}", file=sys.stderr)
        return 1

    print(
        f"magpie-tts[{request_id}]: {len(audio)} bytes -> {args.output} (voice_id={args.voice_id})"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
