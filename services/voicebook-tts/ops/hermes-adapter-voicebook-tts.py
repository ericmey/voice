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
"""

import argparse
import json
import os
import sys
import tempfile
import urllib.error
import urllib.request
from pathlib import Path

p = argparse.ArgumentParser()
p.add_argument("--input", required=True)
p.add_argument("--output", required=True)
p.add_argument("--voice-id", required=True)
p.add_argument("--url", default="http://10.0.20.25:5055/speak")
p.add_argument("--timeout", type=float, default=300.0)
a = p.parse_args()

try:
    text = open(a.input, encoding="utf-8").read().strip()
except OSError as exc:
    sys.exit(f"voicebook-tts: cannot read {a.input}: {exc}")
if not text:
    sys.exit("voicebook-tts: input text is empty")

req = urllib.request.Request(
    a.url,
    data=json.dumps({"voice_id": a.voice_id, "text": text}).encode("utf-8"),
    headers={"Content-Type": "application/json"},
    method="POST",
)

try:
    with urllib.request.urlopen(req, timeout=a.timeout) as resp:
        audio = resp.read()
except urllib.error.HTTPError as exc:
    body = exc.read()[:300].decode("utf-8", "replace")
    sys.exit(f"voicebook-tts: HTTP {exc.code} from {a.url}: {body}")
except Exception as exc:
    sys.exit(f"voicebook-tts: {a.url} unreachable: {exc}")

if len(audio) < 100 or audio[:4] != b"RIFF":
    sys.exit(f"voicebook-tts: response was not WAV ({len(audio)} bytes)")

# ATOMIC WRITE. Writing straight to a.output means a partial write — disk full,
# interruption, crash mid-stream — leaves a truncated file behind while this
# process exits non-zero. Hermes would then see BOTH an error AND an output
# file. The earlier red-proof could never have caught it: unreachable-service
# and unknown-voice both fail before any write happens.
#
# Temp file in the DESTINATION directory (same filesystem, so os.replace is
# atomic), unlinked on every failure path, replaced into place only once the
# bytes are fully on disk.
out = Path(a.output)
tmp = None
try:
    with tempfile.NamedTemporaryFile(
        dir=out.parent, prefix=f".{out.name}.", suffix=".part", delete=False
    ) as fh:
        tmp = Path(fh.name)
        fh.write(audio)
        fh.flush()
        os.fsync(fh.fileno())
    os.replace(tmp, out)
    tmp = None
except OSError as exc:
    sys.exit(f"voicebook-tts: cannot write {a.output}: {exc}")
finally:
    if tmp is not None and tmp.exists():
        tmp.unlink(missing_ok=True)

print(f"voicebook-tts: {len(audio)} bytes -> {a.output} (voice_id={a.voice_id})")
