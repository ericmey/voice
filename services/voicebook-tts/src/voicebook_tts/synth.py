"""Synthesis backends.

``Synthesizer`` is a protocol so the API, registry, limit and error paths are
testable on a laptop with no GPU. ``QwenBaseSynthesizer`` is the real backend
and is only imported inside the service image.
"""

from __future__ import annotations

import io
import wave
from pathlib import Path
from typing import Protocol


class SynthesisError(RuntimeError):
    """Backend failed to produce audio. Never returns partial output."""


class Synthesizer(Protocol):
    """Speak ``text`` in the voice of ``master_path`` (whose words are
    ``reference_transcript``). Returns WAV bytes, never a path."""

    def speak(self, text: str, master_path: Path, reference_transcript: str) -> bytes: ...


def pcm_to_wav_bytes(samples, sample_rate: int) -> bytes:
    """Float32 [-1,1] samples -> 16-bit mono WAV bytes."""
    import numpy as np

    a = np.asarray(samples, dtype=np.float32)
    if a.ndim > 1:
        a = a.squeeze()
    clipped = np.clip(a, -1.0, 1.0)
    pcm = (clipped * 32767.0).astype("<i2")
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(sample_rate)
        w.writeframes(pcm.tobytes())
    return buf.getvalue()


class QwenBaseSynthesizer:
    """Qwen3-TTS Base, loaded ONCE and held resident.

    Model load costs ~5s. A per-message daily-summary path cannot wear that,
    which is the entire reason this service exists rather than a CLI call.
    """

    def __init__(self, model_id: str, device: str = "cuda:0") -> None:
        # Image-only dependencies. Deliberately absent from the workspace so a
        # root `uv sync` never pulls CUDA torch — see docs/AGENT-LESSONS.md.
        import torch  # pyright: ignore[reportMissingImports]
        from qwen_tts import Qwen3TTSModel  # pyright: ignore[reportMissingImports]

        # Fail at construction, not mid-request, if the card cannot run us.
        # A cu124 torch reaching production would otherwise surface as
        # "no kernel image is available" on the first real message.
        if device.startswith("cuda"):
            if not torch.cuda.is_available():
                raise SynthesisError("CUDA unavailable — refusing to start")
            arch = torch.cuda.get_arch_list()
            if "sm_120" not in arch:
                raise SynthesisError(
                    f"torch {torch.__version__} lacks sm_120 kernels (has {arch}). "
                    "A dependency has clobbered the cu128 build. See docs/AGENT-LESSONS.md."
                )

        self._model = Qwen3TTSModel.from_pretrained(
            model_id, device_map=device, dtype=torch.bfloat16, attn_implementation="sdpa"
        )

    def speak(self, text: str, master_path: Path, reference_transcript: str) -> bytes:
        # EVERY backend failure must surface as SynthesisError so the API can
        # return the promised typed 502. Unwrapped, a CUDA OOM or a tokenizer
        # error escapes as a generic 500 — and the test suite would not catch it,
        # because the fake synthesizer raises the correct type while the real one
        # did not. The fake conformed to a contract the real backend broke.
        try:
            wavs, sr = self._model.generate_voice_clone(
                text=text,
                ref_audio=str(master_path),
                ref_text=reference_transcript,
                language="English",
            )
        except SynthesisError:
            raise
        except BaseException as exc:  # noqa: BLE001 - deliberate: type-normalise everything
            raise SynthesisError(f"{type(exc).__name__}: {exc}") from exc

        if not wavs:
            raise SynthesisError("backend returned no audio")

        try:
            return pcm_to_wav_bytes(wavs[0], sr)
        except Exception as exc:
            raise SynthesisError(f"WAV encode failed: {type(exc).__name__}: {exc}") from exc
