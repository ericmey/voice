#!/usr/bin/env python3
"""Probe the production Parakeet/Riva stream with one fixed 16 kHz WAV."""

from __future__ import annotations

import argparse
import asyncio
import json
import re
import time
import wave
from pathlib import Path

from livekit import rtc
from livekit.agents import stt
from livekit.plugins import nvidia


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("wav", type=Path)
    parser.add_argument("--server", default="127.0.0.1:50051")
    parser.add_argument("--model", default="parakeet-1.1b-en-US-asr-streaming")
    parser.add_argument("--pace", action="store_true", help="send audio in real time")
    parser.add_argument("--frame-ms", type=int, default=20)
    parser.add_argument("--reference", help="known transcript for deterministic WER")
    parser.add_argument("--max-wer", type=float, help="fail if WER exceeds this value")
    args = parser.parse_args()
    if args.max_wer is not None and args.reference is None:
        parser.error("--max-wer requires --reference")
    return args


def _read_wav(path: Path) -> tuple[bytes, int]:
    with wave.open(str(path), "rb") as source:
        if source.getnchannels() != 1 or source.getsampwidth() != 2:
            raise ValueError("probe input must be 16-bit mono WAV")
        if source.getframerate() != 16000:
            raise ValueError("probe input must be 16 kHz WAV")
        return source.readframes(source.getnframes()), source.getnframes()


async def _probe(args: argparse.Namespace) -> dict[str, object]:
    pcm, sample_count = _read_wav(args.wav)
    samples_per_frame = 16000 * args.frame_ms // 1000
    bytes_per_frame = samples_per_frame * 2
    adapter = nvidia.STT(
        server=args.server,
        use_ssl=False,
        api_key="",
        model=args.model,
        language_code="en-US",
        sample_rate=16000,
        punctuate=True,
    )

    started = time.perf_counter()
    first_interim_at: float | None = None
    final_at: float | None = None
    transcript = ""
    stream = adapter.stream()

    async def consume() -> None:
        nonlocal first_interim_at, final_at, transcript
        async for event in stream:
            now = time.perf_counter()
            if event.type == stt.SpeechEventType.INTERIM_TRANSCRIPT:
                first_interim_at = first_interim_at or now
            elif event.type == stt.SpeechEventType.FINAL_TRANSCRIPT:
                final_at = now
                transcript = event.alternatives[0].text

    consumer = asyncio.create_task(consume())
    for offset in range(0, len(pcm), bytes_per_frame):
        chunk = pcm[offset : offset + bytes_per_frame]
        frame_samples = len(chunk) // 2
        stream.push_frame(
            rtc.AudioFrame(
                data=chunk,
                sample_rate=16000,
                num_channels=1,
                samples_per_channel=frame_samples,
            )
        )
        if args.pace:
            await asyncio.sleep(frame_samples / 16000)
    audio_end = time.perf_counter()
    stream.end_input()
    await consumer
    await stream.aclose()

    result: dict[str, object] = {
        "file": str(args.wav),
        "server": args.server,
        "model": args.model,
        "audio_duration_ms": round(sample_count / 16, 1),
        "wall_ms": round((final_at - started) * 1000, 1) if final_at else None,
        "first_interim_ms": round((first_interim_at - started) * 1000, 1)
        if first_interim_at
        else None,
        "final_after_audio_ms": round((final_at - audio_end) * 1000, 1) if final_at else None,
        "transcript": transcript,
    }
    if args.reference:
        result["reference"] = args.reference
        result["wer"] = round(_word_error_rate(args.reference, transcript), 4)
    return result


def _normalized_words(text: str) -> list[str]:
    return re.findall(r"[a-z0-9]+", text.lower())


def _word_error_rate(reference: str, hypothesis: str) -> float:
    expected = _normalized_words(reference)
    actual = _normalized_words(hypothesis)
    if not expected:
        return 0.0 if not actual else 1.0
    previous = list(range(len(actual) + 1))
    for row, expected_word in enumerate(expected, start=1):
        current = [row]
        for column, actual_word in enumerate(actual, start=1):
            current.append(
                min(
                    current[-1] + 1,
                    previous[column] + 1,
                    previous[column - 1] + (expected_word != actual_word),
                )
            )
        previous = current
    return previous[-1] / len(expected)


def main() -> None:
    args = _parse_args()
    result = asyncio.run(_probe(args))
    print(json.dumps(result, indent=2))
    if not result["transcript"]:
        raise SystemExit("Parakeet returned no final transcript")
    if args.max_wer is not None and float(result["wer"]) > args.max_wer:
        raise SystemExit(f"WER {result['wer']} exceeds {args.max_wer}")


if __name__ == "__main__":
    main()
