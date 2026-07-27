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
import logging
import os
import wave
from collections.abc import Generator
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

# Frozen wire contract — declared once, imported by the API so it cannot drift.
SAMPLE_RATE = 24000
CHANNELS = 1
SAMPLE_WIDTH = 2  # PCM16
# faster-qwen's native/default streaming window.  The prior 4-step setting
# decoded a new waveform boundary roughly every 320 ms; the exact pre-LiveKit
# capture carried the same intermittent stutters Eric heard on the phone even
# though generation stayed >2x realtime and RTP had zero gaps.  Use the
# engine's 12-step (~1 s) window as the next qualification candidate: fewer
# sliding-decoder joins and more acoustic context per join, while retaining
# realtime streaming.
CHUNK_SIZE = 12
logger = logging.getLogger("voicebook.synth")


def _env_bool(name: str, default: bool) -> bool:
    raw = os.environ.get(name)
    if raw is None:
        return default
    value = raw.strip().lower()
    if value in {"1", "true", "yes", "on"}:
        return True
    if value in {"0", "false", "no", "off"}:
        return False
    raise SynthesisError(f"{name} must be an explicit boolean, got {raw!r}")


def _env_float(name: str, default: float, *, minimum: float, maximum: float) -> float:
    raw = os.environ.get(name)
    if raw is None:
        return default
    try:
        value = float(raw)
    except ValueError as exc:
        raise SynthesisError(f"{name} must be a number, got {raw!r}") from exc
    if not minimum <= value <= maximum:
        raise SynthesisError(f"{name} must be between {minimum} and {maximum}, got {value}")
    return value


@dataclass(frozen=True)
class GenerationOptions:
    """Explicit quality controls for the resident model.

    Defaults preserve the installed faster-qwen3-tts 0.3.2 contract exactly.
    Environment overrides exist so a pinned sidecar can qualify one audio
    variable without editing code or allowing callers to choose generation
    policy per request.
    """

    temperature: float = 0.9
    non_streaming_mode: bool = False

    @classmethod
    def from_env(cls) -> GenerationOptions:
        return cls(
            temperature=_env_float("VOICEBOOK_TEMPERATURE", 0.9, minimum=0.0, maximum=2.0),
            non_streaming_mode=_env_bool("VOICEBOOK_NON_STREAMING_MODE", False),
        )


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

    def __init__(
        self,
        model_path: str,
        device: str = "cuda",
        max_seq_len: int = 2048,
        generation_options: GenerationOptions | None = None,
    ) -> None:
        # Parse policy before importing torch or allocating model VRAM. A typo
        # in a sidecar/deploy setting must fail cheap, not after a 5+ GiB load.
        self._generation_options = generation_options or GenerationOptions.from_env()
        logger.info(
            "generation options temperature=%.3f non_streaming_mode=%s chunk_size=%d",
            self._generation_options.temperature,
            self._generation_options.non_streaming_mode,
            CHUNK_SIZE,
        )

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

    def _generation_kwargs(self) -> dict[str, float | bool]:
        return {
            "temperature": self._generation_options.temperature,
            "non_streaming_mode": self._generation_options.non_streaming_mode,
        }

    def warmup(self, master_path: Path, reference_transcript: str) -> None:
        """Force CUDA-graph capture. Health must stay red until this completes."""
        for _ in self._model.generate_voice_clone_streaming(
            text="warmup",
            language="English",
            ref_audio=str(master_path),
            ref_text=reference_transcript,
            max_new_tokens=32,
            chunk_size=CHUNK_SIZE,
            **self._generation_kwargs(),
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
                **self._generation_kwargs(),
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
