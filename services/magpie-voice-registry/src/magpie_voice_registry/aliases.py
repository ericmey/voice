from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path

from .registry import RegistryError


@dataclass(frozen=True)
class SpeechAliases:
    entries: Mapping[str, str]
    sha256: str

    @property
    def count(self) -> int:
        return len(self.entries)

    def apply(self, text: str) -> str:
        return apply_speech_aliases(text, self.entries)


def empty_speech_aliases() -> SpeechAliases:
    return SpeechAliases(entries={}, sha256=hashlib.sha256(b"{}").hexdigest())


def apply_speech_aliases(text: str, aliases: Mapping[str, str]) -> str:
    spoken = text
    for written, replacement in sorted(aliases.items(), key=lambda item: -len(item[0])):
        pattern = re.compile(rf"(?<!\w){re.escape(written)}(?!\w)", re.IGNORECASE)
        spoken = pattern.sub(replacement, spoken)
    return spoken


def load_speech_aliases(path: str | Path) -> SpeechAliases:
    aliases_path = Path(path)
    try:
        payload = aliases_path.read_bytes()
        raw = json.loads(payload)
    except (OSError, json.JSONDecodeError) as exc:
        raise RegistryError(f"cannot load speech aliases {aliases_path}: {exc}") from exc
    if not isinstance(raw, dict) or not raw:
        raise RegistryError("speech aliases must be a non-empty object")

    entries: dict[str, str] = {}
    for written, spoken in raw.items():
        if not isinstance(written, str) or not written.strip():
            raise RegistryError("every speech alias key must be a non-empty string")
        if not isinstance(spoken, str) or not spoken.strip():
            raise RegistryError(f"{written!r}: speech alias must be a non-empty string")
        entries[written.strip()] = spoken.strip()
    return SpeechAliases(entries=entries, sha256=hashlib.sha256(payload).hexdigest())
