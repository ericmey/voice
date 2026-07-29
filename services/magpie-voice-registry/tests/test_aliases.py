from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest
from magpie_voice_registry.aliases import apply_speech_aliases, load_speech_aliases
from magpie_voice_registry.registry import RegistryError


def test_loads_and_applies_longest_alias_first(tmp_path: Path) -> None:
    path = tmp_path / "aliases.json"
    path.write_text(
        json.dumps(
            {
                "tsumugi-lint": "the lint",
                "Tsumugi": "the documentation standard",
            }
        )
    )
    aliases = load_speech_aliases(path)
    assert aliases.apply("Run TSUMUGI-LINT for Tsumugi.") == (
        "Run the lint for the documentation standard."
    )
    assert aliases.count == 2
    assert aliases.sha256 == hashlib.sha256(path.read_bytes()).hexdigest()


def test_aliases_do_not_replace_inside_larger_words() -> None:
    assert apply_speech_aliases("Tsumugi and preTsumugi", {"Tsumugi": "the doc standard"}) == (
        "the doc standard and preTsumugi"
    )


@pytest.mark.parametrize("payload", [{}, {"Tsumugi": ""}])
def test_rejects_empty_aliases(tmp_path: Path, payload: dict) -> None:
    path = tmp_path / "aliases.json"
    path.write_text(json.dumps(payload))
    with pytest.raises(RegistryError):
        load_speech_aliases(path)
