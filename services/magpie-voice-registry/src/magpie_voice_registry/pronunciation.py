from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path

from .registry import RegistryError


@dataclass(frozen=True)
class PronunciationDictionary:
    entries: Mapping[str, str]
    sha256: str

    @property
    def riva_value(self) -> str:
        return ",".join(f"{word}  {phoneme}" for word, phoneme in self.entries.items())

    @property
    def count(self) -> int:
        return len(self.entries)


def empty_pronunciations() -> PronunciationDictionary:
    return PronunciationDictionary(entries={}, sha256=hashlib.sha256(b"{}").hexdigest())


def load_pronunciations(path: str | Path) -> PronunciationDictionary:
    dictionary_path = Path(path)
    try:
        payload = dictionary_path.read_bytes()
        raw = json.loads(payload)
    except (OSError, json.JSONDecodeError) as exc:
        raise RegistryError(
            f"cannot load pronunciation dictionary {dictionary_path}: {exc}"
        ) from exc
    if not isinstance(raw, dict) or not raw:
        raise RegistryError("pronunciation dictionary must be a non-empty object")

    entries: dict[str, str] = {}
    for grapheme, phoneme in raw.items():
        if not isinstance(grapheme, str) or not grapheme.strip():
            raise RegistryError("every pronunciation grapheme must be a non-empty string")
        if not isinstance(phoneme, str) or not phoneme.strip():
            raise RegistryError(f"{grapheme!r}: phoneme must be a non-empty string")
        if "," in grapheme or "  " in grapheme or "," in phoneme:
            raise RegistryError(
                f"{grapheme!r}: commas and double spaces are reserved by Riva's dictionary wire format"
            )
        entries[grapheme.strip()] = phoneme.strip()
    return PronunciationDictionary(
        entries=entries,
        sha256=hashlib.sha256(payload).hexdigest(),
    )
