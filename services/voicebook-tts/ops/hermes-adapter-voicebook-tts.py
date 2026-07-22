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
import sys
import urllib.error
import urllib.request

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
    sys.exit("voicebook-tts: cannot read %s: %s" % (a.input, exc))
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
    sys.exit("voicebook-tts: HTTP %s from %s: %s" % (exc.code, a.url, body))
except Exception as exc:
    sys.exit("voicebook-tts: %s unreachable: %s" % (a.url, exc))

if len(audio) < 100 or audio[:4] != b"RIFF":
    sys.exit("voicebook-tts: response was not WAV (%d bytes)" % len(audio))

try:
    open(a.output, "wb").write(audio)
except OSError as exc:
    sys.exit("voicebook-tts: cannot write %s: %s" % (a.output, exc))

print("voicebook-tts: %d bytes -> %s (voice_id=%s)" % (len(audio), a.output, a.voice_id))
