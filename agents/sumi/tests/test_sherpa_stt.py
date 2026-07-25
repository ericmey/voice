from array import array

import pytest
from livekit import rtc
from livekit.agents import stt
from sherpa_stt import SherpaSTT, _frame_as_float32_bytes, _result_event_type


def test_pcm16_is_normalized_to_sherpa_float32():
    frame = rtc.AudioFrame(
        data=array("h", [-32768, -16384, 0, 16384, 32767]).tobytes(),
        sample_rate=16000,
        num_channels=1,
        samples_per_channel=5,
    )

    result = array("f")
    result.frombytes(_frame_as_float32_bytes(frame))

    assert result.tolist() == pytest.approx([-1.0, -0.5, 0.0, 0.5, 32767 / 32768])


def test_multichannel_audio_fails_loud():
    frame = rtc.AudioFrame(
        data=array("h", [0, 0]).tobytes(),
        sample_rate=16000,
        num_channels=2,
        samples_per_channel=1,
    )

    with pytest.raises(ValueError, match="requires mono"):
        _frame_as_float32_bytes(frame)


@pytest.mark.parametrize("marker", ["is_final", "is_eof"])
def test_final_markers_map_to_final_transcript(marker):
    assert _result_event_type({marker: True}) == stt.SpeechEventType.FINAL_TRANSCRIPT


def test_nonfinal_result_maps_to_interim_transcript():
    assert _result_event_type({"is_final": False}) == stt.SpeechEventType.INTERIM_TRANSCRIPT


def test_adapter_advertises_true_streaming():
    adapter = SherpaSTT(url="ws://example.invalid:6006")

    assert adapter.capabilities.streaming is True
    assert adapter.capabilities.interim_results is True
    assert adapter.model == "nemotron-speech-streaming-en-0.6b-560ms-int8"
    assert adapter.provider == "sherpa-onnx"
