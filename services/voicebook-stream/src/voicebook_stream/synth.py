"""Synthesis backends for the streaming voicebook service.

Two shapes, one identity:
  - synthesize()        -> completed WAV bytes (Hermes async contract)
  - synthesize_stream() -> generator of raw PCM16 chunks (LiveKit realtime)

Both drive the SAME faster-qwen model on the SAME master. StreamingSynthesizer
is the real backend and is only imported inside the service image; the Protocol
lets the API/registry/lease/cancellation logic be tested on a GPU-free laptop.
"""

from __future__ import annotations

import io
import wave
from collections.abc import Generator
from pathlib import Path
from typing import Protocol

# Frozen wire contract — declared once, imported by the API so it cannot drift.
SAMPLE_RATE = 24000
CHANNELS = 1
SAMPLE_WIDTH = 2  # PCM16
CHUNK_SIZE = 4  # production candidate; chunk=2 is a later optimization


class SynthesisError(RuntimeError):
    """Backend failed. Never yields partial-then-error as success."""


class Synthesizer(Protocol):
    @property
    def ready(self) -> bool:
        """Fail-closed readiness. Health is 503 until this is True."""
        ...

    def synthesize(self, text: str, master_path: Path, reference_transcript: str) -> bytes: ...
    def synthesize_stream(
        self, text: str, master_path: Path, reference_transcript: str
    ) -> Generator[bytes, None, None]: ...


def _f32_to_pcm16(samples) -> bytes:
    import numpy as np

    a = np.asarray(samples, dtype="float32").reshape(-1)
    return (np.clip(a, -1.0, 1.0) * 32767.0).astype("<i2").tobytes()


def pcm16_to_wav(pcm: bytes) -> bytes:
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(CHANNELS)
        w.setsampwidth(SAMPLE_WIDTH)
        w.setframerate(SAMPLE_RATE)
        w.writeframes(pcm)
    return buf.getvalue()


class StreamingSynthesizer:
    """faster-qwen3-tts, CUDA-graph backend, loaded once and held resident."""

    def __init__(self, model_path: str, device: str = "cuda", max_seq_len: int = 2048) -> None:
        import torch  # pyright: ignore[reportMissingImports]
        from faster_qwen3_tts.model import FasterQwen3TTS  # pyright: ignore[reportMissingImports]

        if device.startswith("cuda"):
            if not torch.cuda.is_available():
                raise SynthesisError("CUDA unavailable — refusing to start")
            if "sm_120" not in torch.cuda.get_arch_list():
                raise SynthesisError(
                    f"torch {torch.__version__} lacks sm_120 kernels. See docs/AGENT-LESSONS.md."
                )
        self._model = FasterQwen3TTS.from_pretrained(
            model_path,
            device=device,
            dtype=torch.bfloat16,
            backend="torch",
            max_seq_len=max_seq_len,
            local_files_only=True,
        )
        self._warm = False

    def warmup(self, master_path: Path, reference_transcript: str) -> None:
        """Force CUDA-graph capture. Health must stay red until this completes."""
        for _ in self._model.generate_voice_clone_streaming(
            text="warmup",
            language="English",
            ref_audio=str(master_path),
            ref_text=reference_transcript,
            max_new_tokens=32,
            chunk_size=CHUNK_SIZE,
        ):
            pass
        self._warm = True

    @property
    def ready(self) -> bool:
        return self._warm

    def synthesize_stream(
        self, text: str, master_path: Path, reference_transcript: str
    ) -> Generator[bytes, None, None]:
        """Yield ordered PCM16 chunks. Closing this generator propagates
        GeneratorExit into the nested faster-qwen generator via the finally,
        stopping the GPU pull — that IS cancellation."""
        try:
            gen = self._model.generate_voice_clone_streaming(
                text=text,
                language="English",
                ref_audio=str(master_path),
                ref_text=reference_transcript,
                max_new_tokens=2048,
                chunk_size=CHUNK_SIZE,
            )
        except Exception as exc:
            raise SynthesisError(f"create: {type(exc).__name__}: {exc}") from exc
        try:
            for audio_chunk, sr, _timing in gen:
                if sr != SAMPLE_RATE:
                    raise SynthesisError(
                        f"backend sample rate {sr} != declared {SAMPLE_RATE}; refusing to mislabel"
                    )
                pcm = _f32_to_pcm16(audio_chunk)
                if pcm:  # never emit an empty terminal chunk
                    yield pcm
        except SynthesisError:
            raise
        except Exception as exc:
            # Exceptions raised DURING iteration, not just at creation.
            raise SynthesisError(f"iter: {type(exc).__name__}: {exc}") from exc
        finally:
            # Closes the nested faster-qwen generator on completion, error, or
            # GeneratorExit from a disconnect. Without this, cancelling the
            # OUTER stream would not stop the inner GPU pull.
            gen.close()

    def synthesize(self, text: str, master_path: Path, reference_transcript: str) -> bytes:
        pcm = b"".join(self.synthesize_stream(text, master_path, reference_transcript))
        if not pcm:
            raise SynthesisError("backend produced no audio")
        return pcm16_to_wav(pcm)
