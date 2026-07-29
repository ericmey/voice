from __future__ import annotations

import hashlib
import json
import wave
from dataclasses import dataclass
from pathlib import Path


class RegistryError(RuntimeError):
    """The server-owned voice registry is unsafe or inconsistent."""


class UnknownVoice(KeyError):
    """A caller requested a voice that is not registered."""


@dataclass(frozen=True)
class VoiceSpec:
    voice_id: str
    prompt_path: Path
    sha256: str
    quality: int = 40
    language: str = "en-US"
    sample_rate: int = 22050

    def validate(self) -> None:
        if not self.prompt_path.is_file():
            raise RegistryError(f"{self.voice_id}: prompt missing at {self.prompt_path}")
        actual = hashlib.sha256(self.prompt_path.read_bytes()).hexdigest()
        if actual != self.sha256:
            raise RegistryError(
                f"{self.voice_id}: prompt hash mismatch at {self.prompt_path}; "
                f"expected {self.sha256}, got {actual}"
            )
        if not 1 <= self.quality <= 40:
            raise RegistryError(f"{self.voice_id}: quality must be in 1..40")
        if not self.language:
            raise RegistryError(f"{self.voice_id}: language cannot be empty")
        if not 8000 <= self.sample_rate <= 48000:
            raise RegistryError(f"{self.voice_id}: sample_rate must be in 8000..48000")
        try:
            with wave.open(str(self.prompt_path), "rb") as prompt:
                channels = prompt.getnchannels()
                width = prompt.getsampwidth()
                rate = prompt.getframerate()
                duration = prompt.getnframes() / rate
        except (OSError, EOFError, wave.Error) as exc:
            raise RegistryError(f"{self.voice_id}: prompt is not a readable WAV: {exc}") from exc
        if channels != 1 or width != 2 or rate < 22050 or not 3.0 <= duration <= 10.0:
            raise RegistryError(
                f"{self.voice_id}: prompt must be mono PCM s16, >=22050 Hz, 3-10s; "
                f"got channels={channels}, width={width}, rate={rate}, duration={duration:.3f}s"
            )


class VoiceRegistry:
    def __init__(self, entries: dict[str, VoiceSpec]):
        if not entries:
            raise RegistryError("voice registry is empty")
        self._entries = dict(entries)

    @property
    def voice_ids(self) -> list[str]:
        return sorted(self._entries)

    def get(self, voice_id: str) -> VoiceSpec:
        try:
            return self._entries[voice_id]
        except KeyError:
            raise UnknownVoice(voice_id) from None


def load_registry(path: str | Path) -> VoiceRegistry:
    registry_path = Path(path)
    try:
        raw = json.loads(registry_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise RegistryError(f"cannot load registry {registry_path}: {exc}") from exc
    if not isinstance(raw, dict):
        raise RegistryError("registry root must be an object keyed by voice_id")

    entries: dict[str, VoiceSpec] = {}
    required = {"prompt_path", "sha256"}
    for voice_id, value in raw.items():
        if not isinstance(voice_id, str) or not voice_id or not isinstance(value, dict):
            raise RegistryError("every registry entry must be a named object")
        missing = required - value.keys()
        if missing:
            raise RegistryError(f"{voice_id}: registry entry missing {sorted(missing)}")
        spec = VoiceSpec(
            voice_id=voice_id,
            prompt_path=Path(value["prompt_path"]),
            sha256=value["sha256"],
            quality=int(value.get("quality", 40)),
            language=value.get("language", "en-US"),
            sample_rate=int(value.get("sample_rate", 22050)),
        )
        spec.validate()
        entries[voice_id] = spec
    return VoiceRegistry(entries)
