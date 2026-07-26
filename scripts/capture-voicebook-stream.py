#!/usr/bin/env python3
"""Capture one Voicebook streaming response without changing its audio bytes.

The script records the raw s16le body, a lossless WAV wrapper, and the arrival
time and size of every HTTP chunk.  It is deliberately a client-only
qualification tool: no LiveKit adapter, resampler, codec, or playback scheduler
is involved.
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import time
import wave
from pathlib import Path

import aiohttp

SAMPLE_RATE = 24_000
CHANNELS = 1
SAMPLE_WIDTH_BYTES = 2


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base-url", default="http://voicebook-stream:5060")
    parser.add_argument("--voice-id", default="sumi-v1")
    text = parser.add_mutually_exclusive_group(required=True)
    text.add_argument("--text")
    text.add_argument("--text-file", type=Path)
    parser.add_argument("--output-dir", type=Path, required=True)
    return parser.parse_args()


def load_text(args: argparse.Namespace) -> str:
    value = args.text if args.text is not None else args.text_file.read_text()
    value = value.strip()
    if not value:
        raise ValueError("capture text must not be empty")
    return value


def write_wav(path: Path, pcm: bytes) -> None:
    with wave.open(str(path), "wb") as wav:
        wav.setnchannels(CHANNELS)
        wav.setsampwidth(SAMPLE_WIDTH_BYTES)
        wav.setframerate(SAMPLE_RATE)
        wav.writeframes(pcm)


async def capture(args: argparse.Namespace, text: str) -> None:
    output_dir = args.output_dir
    output_dir.mkdir(parents=True, exist_ok=False)
    url = f"{args.base_url.rstrip('/')}/speak/stream"
    chunks: list[dict[str, int | float | bool]] = []
    body = bytearray()
    started = time.perf_counter()

    timeout = aiohttp.ClientTimeout(total=None, sock_connect=15)
    async with aiohttp.ClientSession(timeout=timeout) as session:
        async with session.post(url, json={"voice_id": args.voice_id, "text": text}) as response:
            response.raise_for_status()
            audio_format = response.headers.get("X-Audio-Format")
            sample_rate = response.headers.get("X-Sample-Rate")
            if audio_format is None:
                raise RuntimeError("Voicebook response omitted required X-Audio-Format header")
            if audio_format.lower() != "s16le":
                raise RuntimeError(
                    f"Voicebook returned X-Audio-Format={audio_format!r}, expected 's16le'"
                )
            if sample_rate is None:
                raise RuntimeError("Voicebook response omitted required X-Sample-Rate header")
            if sample_rate != str(SAMPLE_RATE):
                raise RuntimeError(
                    f"Voicebook returned X-Sample-Rate={sample_rate!r}, expected {SAMPLE_RATE}"
                )
            headers = {
                key: value
                for key, value in response.headers.items()
                if key.lower()
                in {"content-type", "transfer-encoding", "x-audio-format", "x-sample-rate"}
            }
            async for data, end_of_http_chunk in response.content.iter_chunks():
                if not data:
                    continue
                now = time.perf_counter()
                body.extend(data)
                chunks.append(
                    {
                        "index": len(chunks),
                        "arrival_seconds": round(now - started, 6),
                        "size_bytes": len(data),
                        "end_of_http_chunk": end_of_http_chunk,
                    }
                )

    pcm = bytes(body)
    if not pcm:
        raise RuntimeError("Voicebook returned no audio bytes")
    if pcm.startswith(b"RIFF"):
        raise RuntimeError("Voicebook returned a RIFF container, not raw s16le PCM")
    if len(pcm) % (CHANNELS * SAMPLE_WIDTH_BYTES):
        raise RuntimeError(f"unaligned s16le response length: {len(pcm)} bytes")

    raw_path = output_dir / "stream.raw"
    wav_path = output_dir / "stream.wav"
    manifest_path = output_dir / "manifest.json"
    raw_path.write_bytes(pcm)
    write_wav(wav_path, pcm)

    duration_seconds = len(pcm) / (SAMPLE_RATE * CHANNELS * SAMPLE_WIDTH_BYTES)
    manifest = {
        "schema": "voicebook-stream-capture/v1",
        "endpoint": url,
        "voice_id": args.voice_id,
        "text": text,
        "text_sha256": hashlib.sha256(text.encode()).hexdigest(),
        "headers": headers,
        "sample_rate": SAMPLE_RATE,
        "channels": CHANNELS,
        "sample_width_bytes": SAMPLE_WIDTH_BYTES,
        "audio_bytes": len(pcm),
        "audio_sha256": hashlib.sha256(pcm).hexdigest(),
        "audio_duration_seconds": round(duration_seconds, 6),
        "wall_seconds": round(time.perf_counter() - started, 6),
        "chunks": chunks,
    }
    manifest_path.write_text(json.dumps(manifest, indent=2) + "\n")

    print(json.dumps({
        "wav": str(wav_path),
        "manifest": str(manifest_path),
        "audio_seconds": round(duration_seconds, 3),
        "chunks": len(chunks),
        "sha256": manifest["audio_sha256"],
    }))


def main() -> None:
    args = parse_args()
    asyncio.run(capture(args, load_text(args)))


if __name__ == "__main__":
    main()
