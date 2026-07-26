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

RED-PROOFED against cases with a known answer before being trusted:

    input           energy outside 300-3400 Hz    expected
    1 kHz tone                   0.02%            ~0    passes
    6 kHz tone                 100.0%             ~100  removed
    1k + 6k mix                 50.01%            ~50   half each
    100 Hz tone                 99.99%            ~100  below highpass

mu-law SNR measured 35-38 dB, which is the right figure for 8-bit companded
G.711. A filter that reports a plausible number without ever being shown a
signal it must reject is a claim, not an instrument.
"""
from __future__ import annotations

import argparse
import math
import sys
import wave
from pathlib import Path

import numpy as np

# G.711 mu-law, ITU-T G.711. mu=255, 8-bit.
_MU = 255.0
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


def mulaw_encode(audio: np.ndarray) -> np.ndarray:
    """float [-1,1] -> 8-bit mu-law codes. This is the lossy step."""
    a = np.clip(audio, -1.0, 1.0)
    mag = np.log1p(_MU * np.abs(a)) / math.log1p(_MU)
    return np.clip(np.rint(np.sign(a) * mag * 127.0), -127, 127).astype(np.int8)


def mulaw_decode(codes: np.ndarray) -> np.ndarray:
    a = codes.astype(np.float64) / 127.0
    mag = (np.expm1(np.abs(a) * math.log1p(_MU))) / _MU
    return np.sign(a) * mag


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


def main() -> int:
    p = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("input", type=Path)
    p.add_argument("--out", type=Path, default=None)
    p.add_argument("--keep-8k", action="store_true",
                   help="write the true 8 kHz artifact instead of upsampling back")
    p.add_argument("--measure", action="store_true")
    a = p.parse_args()

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
