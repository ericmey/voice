"""Synth contract tests with a fake model — no GPU.

Covers the claims that had NO tests: nested-generator cancellation, ordered
non-empty PCM16 chunks, no empty terminal chunk, valid completed WAV, sample-
rate mismatch refusal, ready=false until warmup.
"""

from __future__ import annotations

import wave
from io import BytesIO
from pathlib import Path

import numpy as np
import pytest
from voicebook_stream.synth import SAMPLE_RATE, StreamingSynthesizer, SynthesisError


class FakeModel:
    """Stands in for FasterQwen3TTS. Records whether its generator was closed."""

    def __init__(self, sr=SAMPLE_RATE, n=4, raise_at=None, trailing_empty=False):
        self.sr, self.n, self.raise_at, self.trailing_empty = sr, n, raise_at, trailing_empty
        self.inner_closed = False

    def generate_voice_clone_streaming(self, **kw):
        def gen():
            try:
                for i in range(self.n):
                    if self.raise_at is not None and i == self.raise_at:
                        raise ValueError("model exploded mid-stream")
                    yield (np.full(1200, 0.1 * (i + 1), dtype=np.float32), self.sr, {})
                if self.trailing_empty:
                    yield (np.zeros(0, dtype=np.float32), self.sr, {})
            finally:
                self.inner_closed = True

        return gen()


def _synth(model):
    s = StreamingSynthesizer.__new__(StreamingSynthesizer)
    s._model = model
    s._warm = False
    return s


REF = Path("/tmp/nonexistent-master.wav")  # never read by the fake


def test_chunks_ordered_nonempty_pcm16_with_value_order():
    s = _synth(FakeModel(n=3))
    chunks = list(s.synthesize_stream("hi", REF, "t"))
    assert len(chunks) == 3
    assert all(len(c) == 1200 * 2 for c in chunks)  # PCM16 = 2 bytes/sample
    # FakeModel emits amplitude 0.1*(i+1) per chunk i — assert ORDER is preserved
    peaks = [max(np.frombuffer(c, dtype="<i2")) for c in chunks]
    assert peaks == sorted(peaks) and len(set(peaks)) == 3, f"chunk order lost: {peaks}"


def test_no_empty_terminal_chunk():
    s = _synth(FakeModel(n=2, trailing_empty=True))
    chunks = list(s.synthesize_stream("hi", REF, "t"))
    assert all(len(c) > 0 for c in chunks), "an empty terminal chunk leaked"


def test_completed_wav_is_valid():
    s = _synth(FakeModel(n=3))
    wav = s.synthesize("hi", REF, "t")
    w = wave.open(BytesIO(wav), "rb")
    assert w.getframerate() == SAMPLE_RATE
    assert w.getnchannels() == 1
    assert w.getsampwidth() == 2
    assert w.getnframes() == 3 * 1200


def test_sample_rate_mismatch_is_refused_not_mislabeled():
    s = _synth(FakeModel(sr=16000))
    with pytest.raises(SynthesisError, match="sample rate"):
        list(s.synthesize_stream("hi", REF, "t"))


def test_disconnect_closes_nested_model_generator():
    m = FakeModel(n=100)
    s = _synth(m)
    stream = s.synthesize_stream("hi", REF, "t")
    next(stream)
    stream.close()  # simulate outer cancellation
    # Verifies the OUTCOME Yua required: closing the outer PCM stream closes
    # the nested faster-qwen generator. Note this is over-determined — the
    # try/except unwinding around the for-loop closes it even without the
    # explicit gen.close(); the explicit close is kept as defensive/portable
    # code (the standalone case proves it IS load-bearing without the wrapping).
    assert m.inner_closed is True, "nested faster-qwen generator not closed"


def test_in_iteration_error_becomes_synthesiserror():
    s = _synth(FakeModel(n=5, raise_at=2))
    with pytest.raises(SynthesisError, match="iter:"):
        list(s.synthesize_stream("hi", REF, "t"))


def test_ready_false_before_warmup():
    s = _synth(FakeModel())
    assert s.ready is False


def test_warmup_sets_ready_true_on_success():
    s = _synth(FakeModel(n=1))
    s.warmup(REF, "t")
    assert s.ready is True


def test_warmup_failure_leaves_ready_false():
    s = _synth(FakeModel(n=5, raise_at=0))
    with pytest.raises(ValueError, match="exploded"):
        s.warmup(REF, "t")
    assert s.ready is False, "ready must stay false if warmup raised"
