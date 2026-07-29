"""Contract tests for Sumi's LiveKit/Riva Magpie Zero Shot extension."""

import asyncio
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast

import pytest
from magpie_tts import MagpieZeroShotTTS

_PCM = b"\x01\x02" * 6000


class _FakeService:
    def __init__(self, error: BaseException | None = None):
        self.calls = []
        self.error = error

    def synthesize_online(self, text, **kwargs):
        self.calls.append((text, kwargs))
        if self.error is not None:
            raise self.error
        return iter((SimpleNamespace(audio=_PCM),))


def _prompt(tmp_path: Path) -> Path:
    path = tmp_path / "prompt.wav"
    path.write_bytes(b"RIFF-test-prompt")
    return path


def _run(coro):
    """Run one async contract without consuming pytest's process-global loop."""

    policy = asyncio.get_event_loop_policy()
    try:
        previous = policy.get_event_loop()
    except RuntimeError:
        previous = asyncio.new_event_loop()
    loop = asyncio.new_event_loop()
    policy.set_event_loop(loop)
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()
        policy.set_event_loop(previous)


def test_contract_is_native_magpie_rate_and_streaming(tmp_path):
    provider = MagpieZeroShotTTS(server="riva:50051", prompt_path=_prompt(tmp_path))

    assert provider.capabilities.streaming is True
    assert provider.sample_rate == 22050
    assert provider.num_channels == 1
    assert provider.model == "magpie-tts-zeroshot"
    assert provider.provider == "nvidia-riva"


def test_missing_prompt_fails_loud(tmp_path):
    with pytest.raises(ValueError, match="does not exist"):
        MagpieZeroShotTTS(server="riva:50051", prompt_path=tmp_path / "missing.wav")


@pytest.mark.parametrize("quality", [0, 41])
def test_quality_outside_riva_contract_fails_loud(tmp_path, quality):
    with pytest.raises(ValueError, match="between 1 and 40"):
        MagpieZeroShotTTS(server="riva:50051", prompt_path=_prompt(tmp_path), quality=quality)


def test_synthesis_passes_zero_shot_fields_and_maps_audio(tmp_path):
    async def go():
        provider = MagpieZeroShotTTS(server="riva:50051", prompt_path=_prompt(tmp_path), quality=27)
        service = _FakeService()
        provider._tts_service = cast(Any, service)
        frames = []
        async for event in provider.synthesize(
            "A sufficiently long sentence for the configured phrase tokenizer to emit cleanly."
        ):
            frames.append(event.frame)
        return provider, service, frames

    provider, service, frames = _run(go())

    assert frames
    assert all(frame.sample_rate == 22050 and frame.num_channels == 1 for frame in frames)
    assert service.calls
    _, kwargs = service.calls[0]
    assert kwargs["voice_name"] is None
    assert kwargs["sample_rate_hz"] == 22050
    assert kwargs["zero_shot_audio_prompt_file"] == str(provider._prompt_path)
    assert kwargs["zero_shot_quality"] == 27


def test_default_quality_uses_accepted_high_quality_setting(tmp_path):
    async def go():
        provider = MagpieZeroShotTTS(server="riva:50051", prompt_path=_prompt(tmp_path))
        service = _FakeService()
        provider._tts_service = cast(Any, service)
        async for _ in provider.synthesize(
            "A sufficiently long sentence for the configured phrase tokenizer to emit cleanly."
        ):
            pass
        return service

    service = _run(go())

    assert service.calls[0][1]["zero_shot_quality"] == 40


def test_worker_error_is_not_silently_swallowed(tmp_path):
    async def go():
        provider = MagpieZeroShotTTS(server="riva:50051", prompt_path=_prompt(tmp_path))
        provider._tts_service = cast(Any, _FakeService(RuntimeError("riva failed")))
        async for _ in provider.synthesize(
            "A sufficiently long sentence for the configured phrase tokenizer to emit cleanly."
        ):
            pass

    with pytest.raises(Exception, match="riva failed"):
        _run(go())
