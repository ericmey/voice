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

Frame geometry is load-bearing: the board runs with `reset=False` so filter
state crosses frame boundaries, and the limiter carries a peak envelope between
frames.

THE LIVE GEOMETRY IS NOT UNIFORM, and this file previously claimed it fed frames
"exactly as tts_node does" while defaulting to 20 ms — a number I picked, not one
I measured. Caught by Yua on review, 2026-07-26. Read from the installed
livekit-agents 1.6.5:

    AudioEmitter.initialize(..., frame_size_ms: int = 200)   tts.py:912
    _TAIL_SAMPLES = self._sample_rate * 10 // 1000           tts.py:1101

and `VoicebookTTS._run()` calls `initialize()` without `frame_size_ms`, so it
takes the 200 ms default. The emitter holds a 10 ms tail so it can tag the final
frame, and combines that tail with the next 200 ms block. So `tts_node` actually
sees:

    190 ms first head, then 200 ms heads, then a variable final frame

`--live-geometry` (the DEFAULT) reproduces that. `--frame-ms N` forces a uniform
size, for the invariance check only.

`--selftest` measures whether the mastering is boundary-invariant instead of
assuming it. If every segmentation yields identical samples, frame size does not
matter and we can say so with evidence rather than with reasoning about
`reset=False`.

  apply-sumi-mastering.py raw.wav --out mastered.wav        # live geometry
  apply-sumi-mastering.py --selftest                        # boundary invariance
  apply-sumi-mastering.py raw.wav --out m.wav --frame-ms 20 # forced uniform
"""
from __future__ import annotations

import argparse
import importlib.util
import os
import sys
import wave
from pathlib import Path

import numpy as np

_AGENT_SRC_ENV = "SUMI_AGENT_SRC"


def _find_agent_py() -> Path:
    """Locate agents/sumi/src/agent.py, unambiguously.

    There is more than one agent.py in this repo (`agents/aoi/`, `agents/sumi/`),
    so a bare `import agent` resolves to whichever directory is first on
    sys.path — which is how this script first loaded the WRONG agent and said so
    in an error message that named the right one. Load by explicit path.
    """
    override = os.environ.get(_AGENT_SRC_ENV)
    if override:
        p = Path(override).expanduser().resolve()
        if not p.is_file():
            raise SystemExit(f"apply-sumi-mastering: {_AGENT_SRC_ENV}={p} is not a file")
        return p

    here = Path(__file__).resolve()
    for base in (here.parent.parent, *here.parents, Path.cwd(), *Path.cwd().parents):
        cand = base / "agents" / "sumi" / "src" / "agent.py"
        if cand.is_file():
            return cand
    raise SystemExit(
        "apply-sumi-mastering: could not find agents/sumi/src/agent.py from "
        f"{here} or {Path.cwd()}. Set {_AGENT_SRC_ENV} to its absolute path."
    )


def _load_processor():
    """Load the live agent's processor by file path, or fail loudly saying why.

    agent.py pulls livekit + pedalboard. If that import fails we must NOT quietly
    substitute anything — a stand-in curve would silently turn this into a test
    of the stand-in.
    """
    agent_py = _find_agent_py()
    sys.path.insert(0, str(agent_py.parent))
    try:
        spec = importlib.util.spec_from_file_location("_sumi_agent_under_test", agent_py)
        if spec is None or spec.loader is None:
            raise ImportError(f"no loader for {agent_py}")
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        return mod._TelephonyMasteringProcessor, mod._master_audio_frame
    except Exception as exc:  # noqa: BLE001 - the message matters more than the type
        raise SystemExit(
            f"apply-sumi-mastering: cannot load the live mastering from\n"
            f"  {agent_py}\n"
            f"  ({type(exc).__name__}: {exc})\n"
            f"  Run this with the agent's own environment — on mizuki that is\n"
            f"  inside the container: docker exec -w /app voice-agent-sumi "
            f"/app/.venv/bin/python ...\n"
            f"  REFUSING to substitute a reimplementation — that would make this "
            f"a test of the substitute."
        ) from exc


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


def live_segments(n_samples: int, rate: int) -> list[int]:
    """Frame lengths as the live AudioEmitter actually produces them.

    200 ms blocks from AudioByteStream, minus a 10 ms tail held back so the last
    frame can be tagged is_final, that tail prepended to the next block. Net:
    190 ms first head, 200 ms thereafter, variable final.
    """
    block = rate * 200 // 1000
    tail = rate * 10 // 1000
    segs: list[int] = []
    pending = 0
    remaining = n_samples
    while remaining > 0:
        take = min(block, remaining)
        remaining -= take
        combined = pending + take
        if remaining <= 0:                 # final flush: emit everything held
            segs.append(combined)
            break
        segs.append(combined - tail)
        pending = tail
    return [s for s in segs if s > 0]


def master(pcm: np.ndarray, rate: int, segments: list[int], Processor, master_frame,
           rtc) -> np.ndarray:
    """Feed `pcm` through the SHIPPED mastering in the given frame lengths."""
    processor = Processor()
    out_chunks: list[np.ndarray] = []
    pos = 0
    for n in segments:
        chunk = pcm[pos:pos + n]
        pos += n
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
    return np.concatenate(out_chunks) if out_chunks else np.array([], dtype="<i2")


def selftest() -> int:
    """Is the mastering boundary-invariant? Measure it; do not reason about it.

    `reset=False` and a carried peak envelope are REASONS to expect invariance.
    They are not evidence of it. If every segmentation gives identical samples,
    frame size does not matter and the fixture's default is a cosmetic choice. If
    they differ, only the live geometry is admissible.
    """
    Processor, master_frame = _load_processor()
    from livekit import rtc  # type: ignore

    rate = 24000
    n = rate * 3
    # Deterministic, wideband, and loud enough to engage the limiter — a quiet
    # signal would leave the limiter idle and prove nothing about the one stage
    # that carries state between frames.
    t = np.arange(n) / rate
    sig = (0.55 * np.sin(2 * np.pi * 220 * t)
           + 0.30 * np.sin(2 * np.pi * 1830 * t)
           + 0.20 * np.sin(2 * np.pi * 3400 * t))
    sig[rate // 2: rate // 2 + 400] += 0.9          # transient, to spike the limiter
    pcm = np.clip(np.rint(sig * 32767), -32768, 32767).astype("<i2")

    cases: list[tuple[str, list[int]]] = [
        ("live (190/200/final)", live_segments(n, rate)),
        ("uniform 200 ms", [rate * 200 // 1000] * (n // (rate * 200 // 1000))),
        ("uniform 20 ms", [rate * 20 // 1000] * (n // (rate * 20 // 1000))),
        ("uniform 10 ms", [rate * 10 // 1000] * (n // (rate * 10 // 1000))),
        ("single frame", [n]),
    ]

    ref_name, ref_segs = cases[0]
    ref = master(pcm, rate, ref_segs, Processor, master_frame, rtc)
    print(f"  reference: {ref_name}  ({len(ref_segs)} frames, {len(ref)} samples)")

    ok = True
    for name, segs in cases[1:]:
        got = master(pcm, rate, segs, Processor, master_frame, rtc)
        m = min(len(ref), len(got))
        if m == 0:
            print(f"    {name:22} SKIP  (no full frames)")
            continue
        diff = np.abs(ref[:m].astype(np.int32) - got[:m].astype(np.int32))
        nz = int(np.count_nonzero(diff))
        print(f"    {name:22} max_delta={int(diff.max()):>6} lsb   "
              f"differing={nz}/{m}")
        if nz:
            ok = False

    print()
    if ok:
        print("  BOUNDARY-INVARIANT — segmentation does not change the samples.")
    else:
        print("  NOT boundary-invariant — only --live-geometry is admissible for")
        print("  the A/B, and any uniform-frame result is measuring the fixture.")
    return 0 if ok else 1


def main() -> int:
    p = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("input", type=Path, nargs="?")
    p.add_argument("--out", type=Path, default=None)
    p.add_argument("--frame-ms", type=int, default=None,
                   help="force a UNIFORM frame size; default is live geometry")
    p.add_argument("--selftest", action="store_true",
                   help="measure whether the mastering is boundary-invariant")
    a = p.parse_args()

    if a.selftest:
        print("\napply-sumi-mastering selftest — frame-boundary invariance\n")
        return selftest()

    if a.input is None or a.out is None:
        print("apply-sumi-mastering: input and --out are required", file=sys.stderr)
        return 2
    if not a.input.is_file():
        print(f"apply-sumi-mastering: no such file {a.input}", file=sys.stderr)
        return 2

    Processor, master_frame = _load_processor()
    from livekit import rtc  # type: ignore  # already proven importable above

    pcm, rate = read_wav(a.input)
    if a.frame_ms:
        n_per_frame = max(1, rate * a.frame_ms // 1000)
        segs = [n_per_frame] * (len(pcm) // n_per_frame)
        if len(pcm) % n_per_frame:
            segs.append(len(pcm) % n_per_frame)
        geometry = f"uniform {a.frame_ms} ms"
    else:
        segs = live_segments(len(pcm), rate)
        geometry = "live (190 ms head, 200 ms, variable final)"

    mastered = master(pcm, rate, segs, Processor, master_frame, rtc)
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
    print(f"    rate         {rate} Hz, {len(pcm)} samples, {len(pcm)/rate:.2f}s")
    print(f"    geometry     {geometry}, {len(segs)} frames")
    print(f"    peak in      {dbfs(pcm):.2f} dBFS")
    print(f"    peak out     {dbfs(mastered):.2f} dBFS   (limiter ceiling is -3.00)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
