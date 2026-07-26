#!/usr/bin/env python3
"""phone-leg-transform — put audio through what the PHONE actually does to it.

Built 2026-07-26. Every call on this trunk negotiates `PCMU/8000` — G.711 mu-law
at 8 kHz (confirmed in livekit-sip `using codecs` logs, six inbound calls,
2026-07-25, no exceptions). We synthesise at 24 kHz.

    G.711 passes roughly 300-3400 Hz and quantises to 8-bit companded samples.
    Everything above ~4 kHz is gone before the caller hears it.

So grading TTS quality on the 24 kHz file grades audio no caller has ever heard.
This applies the transform the phone leg applies, so an A/B can be run on the
signal that actually reaches Eric's ear.

WHAT THIS IS NOT: it is not proof that the codec causes any artifact. It is the
control that tells you WHICH SIDE of the codec an artifact lives on.

    artifact present in the input, absent after  -> not the phone leg
    artifact absent in the input, present after  -> the ceiling is the codec,
                                                    and the fix is the trunk,
                                                    not the synthesiser

  phone-leg-transform.py in.wav --out phoned.wav
  phone-leg-transform.py in.wav --out phoned.wav --keep-8k   # true 8 kHz file
  phone-leg-transform.py in.wav --measure                    # numbers only

`--keep-8k` writes the real 8 kHz artifact. The default upsamples back to the
input rate so an A/B plays at matched rate — the resampling back adds nothing
that was lost, it only makes the two files comparable in a player.

Needs numpy. On mizuki use `~/voice/.venv/bin/python`; system python3 has none.
mu-law is implemented here in numpy rather than via `audioop` on purpose —
audioop was removed in Python 3.13, and a fixture that dies on the next
interpreter bump is not a fixture.

TWO STAGES, TWO SEPARATE PROOFS. Proving one and claiming the instrument is
exactly the mistake this file already made once.

  BANDPASS — tone tests with a known answer:

    input           energy outside 300-3400 Hz    expected
    1 kHz tone                   0.02%            ~0    passes
    6 kHz tone                 100.0%             ~100  removed
    1k + 6k mix                 50.01%            ~50   half each
    100 Hz tone                 99.99%            ~100  below highpass

  CODEC — `--selftest`, bit-exact against ffmpeg's pcm_mulaw:

    vectors      9 hand-checked PCM16 values incl. 0, +-1, full scale
    exhaustive   all 65536 int16 inputs, 0 mismatches
    decode       all 256 codes back to linear, 0 mismatches

The first version of this file passed the bandpass tests with a codec that was
NOT G.711 — an idealised `log1p(mu*x)` curve, which has the right shape and the
wrong quantisation levels, and therefore reported a believable 35-38 dB SNR
while not being the transfer function the trunk applies. Caught by Yua on
review, 2026-07-26, with the vector that settles it in one line: G.711 encodes
silence to 0xFF; the idealised curve encoded it to 0x00.

A stage you did not test is a stage you are guessing about, however good the
number from the stage you did test looks.
"""
from __future__ import annotations

import argparse
import math
import sys
import wave
from pathlib import Path

import numpy as np

# Telephony passband. The lowpass sits below Nyquist for 8 kHz (4000 Hz) so
# decimation cannot alias — a real gateway filters before it decimates, and
# skipping that step would inject distortion the phone leg does NOT have.
_LOWPASS_HZ = 3400.0
_HIGHPASS_HZ = 300.0


def read_wav(path: Path) -> tuple[np.ndarray, int]:
    with wave.open(str(path), "rb") as w:
        if w.getsampwidth() != 2:
            raise SystemExit(f"{path}: expected PCM16, got sampwidth={w.getsampwidth()}")
        rate = w.getframerate()
        ch = w.getnchannels()
        pcm = np.frombuffer(w.readframes(w.getnframes()), dtype="<i2")
    if ch > 1:
        pcm = pcm.reshape(-1, ch).mean(axis=1).astype("<i2")
    return pcm.astype(np.float64) / 32768.0, rate


def write_wav(path: Path, audio: np.ndarray, rate: int) -> None:
    pcm = np.clip(np.rint(audio * 32768.0), -32768, 32767).astype("<i2")
    with wave.open(str(path), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(rate)
        w.writeframes(pcm.tobytes())


def _fir_lowpass(cutoff_hz: float, rate: int, taps: int = 255) -> np.ndarray:
    """Windowed-sinc lowpass. Blackman window, linear phase."""
    if taps % 2 == 0:
        taps += 1
    fc = cutoff_hz / rate               # normalised, cycles/sample
    n = np.arange(taps) - (taps - 1) / 2
    h = 2 * fc * np.sinc(2 * fc * n)
    h *= np.blackman(taps)
    return h / h.sum()


def _fir_highpass(cutoff_hz: float, rate: int, taps: int = 255) -> np.ndarray:
    """Spectral inversion of the matching lowpass."""
    lp = _fir_lowpass(cutoff_hz, rate, taps)
    hp = -lp
    hp[(len(hp) - 1) // 2] += 1.0
    return hp


def band_limit(audio: np.ndarray, rate: int) -> np.ndarray:
    """Apply the telephony passband BEFORE decimation, as a gateway does."""
    y = np.convolve(audio, _fir_lowpass(_LOWPASS_HZ, rate), mode="same")
    return np.convolve(y, _fir_highpass(_HIGHPASS_HZ, rate), mode="same")


def resample(audio: np.ndarray, src: int, dst: int) -> np.ndarray:
    """Linear-interpolation resample. The anti-alias filtering is done by
    band_limit() first, so this only has to move the sample grid."""
    if src == dst:
        return audio
    n_out = int(round(len(audio) * dst / src))
    x_out = np.arange(n_out) * (src / dst)
    return np.interp(x_out, np.arange(len(audio)), audio)


# G.711 PCMU is NOT continuous log companding. It is piecewise-linear: sign bit,
# 3-bit exponent selecting one of 8 segments, 4-bit mantissa, whole byte
# inverted. An idealised log curve has the same SHAPE and different quantisation
# levels, so it yields a believable SNR while being the wrong transfer function.
# The decoder below is the ITU-T/Sun reference; the encoder is built from it,
# and both are checked bit-for-bit against ffmpeg by --selftest.


def _g711_decode_u8(codes: np.ndarray) -> np.ndarray:
    """8-bit PCMU codes -> int16."""
    u = (~codes.astype(np.int32)) & 0xFF
    t = (((u & 0x0F) << 3) + 0x84) << ((u & 0x70) >> 4)
    return np.where(u & 0x80, 0x84 - t, t - 0x84).astype(np.int16)


def _build_encode_table() -> np.ndarray:
    """Build the 16384-entry linear->PCMU table the way ffmpeg does.

    IMPORTANT, and the reason the obvious implementation disagrees: the ITU
    reference encoder TRUNCATES within a segment, while ffmpeg derives its
    encode table by inverting the decoder and taking the MIDPOINT between
    adjacent output levels — i.e. nearest-neighbour quantisation. The two agree
    on 99.2% of int16 inputs and differ by one code on the 512 samples that sit
    exactly on a decision boundary.

    We match ffmpeg because ffmpeg is the ground truth we can actually run.
    That difference is at most one quantisation step, but it is a real
    difference and it is named here rather than rounded away in a comment.
    """
    mask = 0xFF
    table = np.empty(16384, dtype=np.uint8)
    levels = _g711_decode_u8(np.arange(256, dtype=np.uint8)).astype(np.int32)

    table[8192] = mask
    j = 1
    for i in range(127):
        v1 = int(levels[i ^ mask])
        v2 = int(levels[(i + 1) ^ mask])
        v = (v1 + v2 + 4) >> 3
        if v > j:
            table[8192 - np.arange(j, v)] = i ^ (mask ^ 0x80)
            table[8192 + np.arange(j, v)] = i ^ mask
            j = v
    if j < 8192:
        table[8192 - np.arange(j, 8192)] = 127 ^ (mask ^ 0x80)
        table[8192 + np.arange(j, 8192)] = 127 ^ mask
    table[0] = table[1]
    return table


_ENCODE_TABLE = _build_encode_table()


def _g711_encode_i16(pcm: np.ndarray) -> np.ndarray:
    """int16 -> 8-bit PCMU codes. Table lookup on the 14-bit value, as ffmpeg."""
    idx = (pcm.astype(np.int32) >> 2) + 8192
    return _ENCODE_TABLE[np.clip(idx, 0, 16383)]


def mulaw_encode(audio: np.ndarray) -> np.ndarray:
    """float [-1,1] -> 8-bit PCMU codes. This is the lossy step."""
    pcm = np.clip(np.rint(np.clip(audio, -1.0, 1.0) * 32768.0), -32768, 32767).astype(np.int16)
    return _g711_encode_i16(pcm)


def mulaw_decode(codes: np.ndarray) -> np.ndarray:
    return _g711_decode_u8(codes).astype(np.float64) / 32768.0


def phone_leg(audio: np.ndarray, rate: int) -> tuple[np.ndarray, np.ndarray]:
    """Return (audio_at_8k_after_codec, codes). The full one-way transform."""
    limited = band_limit(audio, rate)
    at8k = resample(limited, rate, 8000)
    codes = mulaw_encode(at8k)
    return mulaw_decode(codes), codes


def measure(before: np.ndarray, after8k: np.ndarray, rate: int) -> dict:
    """Numbers that survive resampling — compare like with like at 8 kHz."""
    ref8k = resample(band_limit(before, rate), rate, 8000)
    n = min(len(ref8k), len(after8k))
    ref, got = ref8k[:n], after8k[:n]
    err = got - ref

    def db(x):
        return 20 * math.log10(x) if x > 1e-12 else float("-inf")

    # Energy discarded by band-limiting alone — the part no codec setting can
    # recover, because it is outside the passband the carrier will carry.
    full_energy = float(np.sum(before**2))
    band_energy = float(np.sum(band_limit(before, rate) ** 2))
    lost_frac = 1.0 - (band_energy / full_energy) if full_energy > 0 else 0.0

    return {
        "input_rate": rate,
        "input_peak_dbfs": round(db(float(np.max(np.abs(before)))), 2),
        "energy_outside_300_3400hz_pct": round(lost_frac * 100, 2),
        "codec_snr_db": round(db(float(np.sqrt(np.mean(ref**2))))
                              - db(float(np.sqrt(np.mean(err**2)))), 2),
        "codec_peak_err_dbfs": round(db(float(np.max(np.abs(err)))), 2),
        "clipped_samples": int(np.sum(np.abs(before) >= 0.999)),
    }


def selftest() -> int:
    """Prove the codec stage against the REAL encoder, not against its shape.

    Two checks. The vector check is human-readable and catches a codebook swap
    instantly (G.711 silence is 0xFF; an idealised curve gives 0x00). The
    exhaustive check is the one that actually settles it — all 65536 int16
    inputs, byte for byte, against ffmpeg's pcm_mulaw.
    """
    import shutil
    import subprocess

    ok = True

    # Yua's review vectors, 2026-07-26.
    probe = np.array([-32768, -20000, -1000, -1, 0, 1, 1000, 20000, 32767], dtype=np.int16)
    want = np.array([0, 12, 78, 126, 255, 255, 206, 140, 128], dtype=np.uint8)
    got = _g711_encode_i16(probe)
    match = bool(np.array_equal(got, want))
    print(f"  vectors      {'PASS' if match else 'FAIL'}")
    if not match:
        print(f"    input    {probe.tolist()}")
        print(f"    expected {want.tolist()}")
        print(f"    got      {got.tolist()}")
        ok = False

    ffmpeg = shutil.which("ffmpeg")
    if not ffmpeg:
        print("  exhaustive   SKIPPED — no ffmpeg on PATH")
        print("               A SKIPPED check is not a passed check.")
        return 0 if ok else 1

    every = np.arange(-32768, 32768, dtype=np.int16)
    proc = subprocess.run(
        [ffmpeg, "-hide_banner", "-loglevel", "error",
         "-f", "s16le", "-ar", "8000", "-ac", "1", "-i", "pipe:0",
         "-f", "mulaw", "-acodec", "pcm_mulaw", "pipe:1"],
        input=every.tobytes(), capture_output=True, check=True,
    )
    ref = np.frombuffer(proc.stdout, dtype=np.uint8)
    mine = _g711_encode_i16(every)
    n = min(len(ref), len(mine))
    bad = int(np.count_nonzero(ref[:n] != mine[:n]))
    print(f"  exhaustive   {'PASS' if bad == 0 else 'FAIL'}  "
          f"({n} int16 inputs vs ffmpeg pcm_mulaw, {bad} mismatches)")
    if bad:
        idx = int(np.flatnonzero(ref[:n] != mine[:n])[0])
        print(f"    first at pcm={every[idx]}: ffmpeg={ref[idx]} mine={mine[idx]}")
        ok = False

    # Decode must invert ffmpeg's encode, not just our own.
    back = _g711_decode_u8(ref[:n])
    proc2 = subprocess.run(
        [ffmpeg, "-hide_banner", "-loglevel", "error",
         "-f", "mulaw", "-ar", "8000", "-ac", "1", "-i", "pipe:0",
         "-f", "s16le", "-acodec", "pcm_s16le", "pipe:1"],
        input=ref[:n].tobytes(), capture_output=True, check=True,
    )
    ref_back = np.frombuffer(proc2.stdout, dtype="<i2")
    m = min(len(ref_back), len(back))
    bad2 = int(np.count_nonzero(ref_back[:m] != back[:m]))
    print(f"  decode       {'PASS' if bad2 == 0 else 'FAIL'}  ({m} codes, {bad2} mismatches)")
    if bad2:
        ok = False

    return 0 if ok else 1


def main() -> int:
    p = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("input", type=Path, nargs="?")
    p.add_argument("--selftest", action="store_true",
                   help="validate the codec stage against ffmpeg pcm_mulaw and exit")
    p.add_argument("--out", type=Path, default=None)
    p.add_argument("--keep-8k", action="store_true",
                   help="write the true 8 kHz artifact instead of upsampling back")
    p.add_argument("--measure", action="store_true")
    a = p.parse_args()

    if a.selftest:
        print("\nphone-leg-transform selftest — G.711 PCMU codec stage\n")
        return selftest()

    if a.input is None:
        print("phone-leg-transform: an input file is required", file=sys.stderr)
        return 2
    if not a.input.is_file():
        print(f"phone-leg-transform: no such file {a.input}", file=sys.stderr)
        return 2

    audio, rate = read_wav(a.input)
    decoded8k, codes = phone_leg(audio, rate)

    if a.measure or not a.out:
        m = measure(audio, decoded8k, rate)
        width = max(len(k) for k in m)
        print(f"\n  {a.input.name}")
        for k, v in m.items():
            print(f"    {k:<{width}}  {v}")
        print(f"\n    {len(codes)} mu-law samples @ 8000 Hz "
              f"({len(codes)/8000:.2f}s)")
        if m["energy_outside_300_3400hz_pct"] > 20:
            print(f"\n    NOTE {m['energy_outside_300_3400hz_pct']}% of input energy sits "
                  "outside the telephony passband.\n"
                  "         The caller never hears it, at any TTS quality setting.")

    if a.out:
        out_rate = 8000 if a.keep_8k else rate
        write_wav(a.out, resample(decoded8k, 8000, out_rate), out_rate)
        print(f"\n  wrote {a.out} @ {out_rate} Hz")

    return 0


if __name__ == "__main__":
    sys.exit(main())
