#!/usr/bin/env python3
"""apply-sumi-mastering — run the SHIPPED telephony mastering over a WAV offline.

The middle capture point of the phone-leg A/B:

    (a) raw 24 kHz TTS            scripts/capture-voicebook-stream.py   [canonical]
    (b) after mastering           THIS SCRIPT
    (c) after the phone leg       scripts/phone-leg-transform.py

This imports `_TelephonyMasteringProcessor` from `agents/sumi/src/agent.py` and
drives it with the same frame geometry the live agent uses. It does NOT
reimplement the curve.

That is the whole point. A reimplementation would be a second instrument to
verify, and on 2026-07-26 three separate defects in this repo's new tooling were
all the same shape — a measurement that was true of something narrower than its
label. The way not to have that problem again is to not build a second copy of
the thing under test. If this script and the agent ever disagree, the A/B is
measuring the script.

Frame geometry is load-bearing. The mastering board runs with `reset=False` so
filter state crosses frame boundaries, and the limiter carries a peak envelope
between frames. Feeding one giant frame would produce audio the live path never
produces — so this chops the input into LiveKit-sized frames and feeds them in
order, exactly as `tts_node` does.

  apply-sumi-mastering.py raw.wav --out mastered.wav
  apply-sumi-mastering.py raw.wav --out mastered.wav --frame-ms 10
"""
from __future__ import annotations

import argparse
import sys
import wave
from pathlib import Path

import numpy as np

_REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_REPO / "agents" / "sumi" / "src"))


def _load_processor():
    """Import the live agent's processor, or fail loudly saying why.

    Importing agent.py pulls livekit + pedalboard. If that import fails we must
    NOT quietly substitute anything — a stand-in curve would silently turn this
    into a test of the stand-in.
    """
    try:
        from agent import _TelephonyMasteringProcessor, _master_audio_frame  # type: ignore
    except Exception as exc:  # noqa: BLE001 - the message matters more than the type
        raise SystemExit(
            f"apply-sumi-mastering: cannot import the live mastering from "
            f"agents/sumi/src/agent.py ({type(exc).__name__}: {exc})\n"
            f"  Run this with the agent's environment, e.g. mizuki's "
            f"~/voice/.venv/bin/python.\n"
            f"  REFUSING to substitute a reimplementation — that would make this "
            f"a test of the substitute."
        ) from exc
    return _TelephonyMasteringProcessor, _master_audio_frame


def read_wav(path: Path) -> tuple[np.ndarray, int]:
    with wave.open(str(path), "rb") as w:
        if w.getsampwidth() != 2:
            raise SystemExit(f"{path}: expected PCM16, got sampwidth={w.getsampwidth()}")
        if w.getnchannels() != 1:
            raise SystemExit(f"{path}: expected mono, got {w.getnchannels()} channels")
        return np.frombuffer(w.readframes(w.getnframes()), dtype="<i2"), w.getframerate()


def write_wav(path: Path, pcm: np.ndarray, rate: int) -> None:
    with wave.open(str(path), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(rate)
        w.writeframes(pcm.astype("<i2").tobytes())


def main() -> int:
    p = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("input", type=Path)
    p.add_argument("--out", type=Path, required=True)
    p.add_argument("--frame-ms", type=int, default=20,
                   help="LiveKit frame size to simulate; state crosses frames")
    a = p.parse_args()

    if not a.input.is_file():
        print(f"apply-sumi-mastering: no such file {a.input}", file=sys.stderr)
        return 2

    Processor, master_frame = _load_processor()
    from livekit import rtc  # type: ignore  # already proven importable above

    pcm, rate = read_wav(a.input)
    n_per_frame = max(1, rate * a.frame_ms // 1000)

    processor = Processor()
    out_chunks: list[np.ndarray] = []
    for start in range(0, len(pcm), n_per_frame):
        chunk = pcm[start:start + n_per_frame]
        if len(chunk) == 0:
            continue
        frame = rtc.AudioFrame(
            data=chunk.tobytes(),
            sample_rate=rate,
            num_channels=1,
            samples_per_channel=len(chunk),
        )
        out = master_frame(frame, processor)
        out_chunks.append(np.frombuffer(out.data, dtype="<i2"))

    mastered = np.concatenate(out_chunks) if out_chunks else np.array([], dtype="<i2")
    if len(mastered) != len(pcm):
        raise SystemExit(
            f"apply-sumi-mastering: length changed {len(pcm)} -> {len(mastered)}; "
            "the live path preserves frame geometry, so this is a bug here"
        )

    write_wav(a.out, mastered, rate)

    def dbfs(x: np.ndarray) -> float:
        peak = float(np.max(np.abs(x))) / 32768.0 if len(x) else 0.0
        return 20 * np.log10(peak) if peak > 0 else float("-inf")

    print(f"\n  {a.input.name} -> {a.out.name}")
    print(f"    rate         {rate} Hz, {len(pcm)} samples, "
          f"{len(pcm)/rate:.2f}s, {a.frame_ms} ms frames ({n_per_frame} samples)")
    print(f"    peak in      {dbfs(pcm):.2f} dBFS")
    print(f"    peak out     {dbfs(mastered):.2f} dBFS   (limiter ceiling is -3.00)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
