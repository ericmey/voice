"""Regression tests for Sumi's accepted Pedalboard phone-mastering curve."""

import numpy as np
from agent import (
    _MASTERING_ENABLED,
    _MASTERING_PEAK_LIMIT,
    _build_telephony_mastering_board,
    _master_audio_frame,
)
from livekit import rtc

_SAMPLE_RATE = 24000


def _frame(samples: np.ndarray) -> rtc.AudioFrame:
    pcm = np.clip(np.rint(samples * 32768.0), -32768.0, 32767.0).astype(np.int16)
    return rtc.AudioFrame(
        data=pcm.tobytes(),
        sample_rate=_SAMPLE_RATE,
        num_channels=1,
        samples_per_channel=pcm.size,
    )


def _float_samples(frame: rtc.AudioFrame) -> np.ndarray:
    return np.frombuffer(frame.data, dtype=np.int16).astype(np.float32) / 32768.0


def test_mastering_is_enabled_by_default_and_preserves_frame_contract():
    samples = 0.1 * np.sin(2.0 * np.pi * 900.0 * np.arange(2400) / _SAMPLE_RATE)
    source = _frame(samples)

    mastered = _master_audio_frame(source, _build_telephony_mastering_board())

    assert _MASTERING_ENABLED is True
    assert mastered.sample_rate == source.sample_rate
    assert mastered.num_channels == source.num_channels
    assert mastered.samples_per_channel == source.samples_per_channel
    assert bytes(mastered.data) != bytes(source.data)
    assert np.max(np.abs(_float_samples(mastered))) <= _MASTERING_PEAK_LIMIT + (1 / 32768)


def test_sample_b_curve_reduces_low_mud_and_increases_presence():
    duration_s = 2
    times = np.arange(_SAMPLE_RATE * duration_s) / _SAMPLE_RATE
    frequencies = (100, 350, 900, 2400)
    samples = np.sum(
        np.stack([0.04 * np.sin(2.0 * np.pi * frequency * times) for frequency in frequencies]),
        axis=0,
    )
    source = _frame(samples)
    mastered = _master_audio_frame(source, _build_telephony_mastering_board())

    source_fft = np.abs(np.fft.rfft(_float_samples(source)))
    mastered_fft = np.abs(np.fft.rfft(_float_samples(mastered)))
    bins = np.fft.rfftfreq(source.samples_per_channel, 1.0 / _SAMPLE_RATE)

    gains = {}
    for frequency in frequencies:
        index = int(np.argmin(np.abs(bins - frequency)))
        gains[frequency] = mastered_fft[index] / source_fft[index]

    assert gains[100] < gains[350] < gains[900] < gains[2400]
    assert gains[350] < 1.0
    assert gains[2400] > 1.0


def test_overload_is_contained_without_int16_wraparound(caplog):
    samples = 0.95 * np.sin(2.0 * np.pi * 2400.0 * np.arange(2400) / _SAMPLE_RATE)

    mastered = _master_audio_frame(_frame(samples), _build_telephony_mastering_board())
    output = _float_samples(mastered)

    assert np.max(np.abs(output)) <= _MASTERING_PEAK_LIMIT + (1 / 32768)
    assert "overload contained" in caplog.text
