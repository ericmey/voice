from __future__ import annotations

import hashlib
import json
import wave
from pathlib import Path

import pytest
from magpie_voice_registry.registry import RegistryError, load_registry


def make_prompt(path: Path, *, seconds: float = 5.0, rate: int = 22050) -> str:
    with wave.open(str(path), "wb") as wav:
        wav.setnchannels(1)
        wav.setsampwidth(2)
        wav.setframerate(rate)
        wav.writeframes(b"\0\0" * int(rate * seconds))
    return hashlib.sha256(path.read_bytes()).hexdigest()


def write_registry(path: Path, prompt: Path, digest: str, **extra) -> None:
    path.write_text(
        json.dumps({"sumi-v1": {"prompt_path": str(prompt), "sha256": digest, **extra}})
    )


def test_loads_checked_prompt(tmp_path: Path) -> None:
    prompt = tmp_path / "sumi.wav"
    digest = make_prompt(prompt)
    registry_path = tmp_path / "registry.json"
    write_registry(registry_path, prompt, digest, quality=40)
    spec = load_registry(registry_path).get("sumi-v1")
    assert spec.quality == 40
    assert spec.prompt_path == prompt


def test_hash_mismatch_fails_loud(tmp_path: Path) -> None:
    prompt = tmp_path / "sumi.wav"
    make_prompt(prompt)
    registry_path = tmp_path / "registry.json"
    write_registry(registry_path, prompt, "0" * 64)
    with pytest.raises(RegistryError, match="hash mismatch"):
        load_registry(registry_path)


@pytest.mark.parametrize("seconds", [2.9, 10.1])
def test_prompt_duration_contract(tmp_path: Path, seconds: float) -> None:
    prompt = tmp_path / "sumi.wav"
    digest = make_prompt(prompt, seconds=seconds)
    registry_path = tmp_path / "registry.json"
    write_registry(registry_path, prompt, digest)
    with pytest.raises(RegistryError, match="3-10s"):
        load_registry(registry_path)
