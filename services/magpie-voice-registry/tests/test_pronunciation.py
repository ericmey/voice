from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest
from magpie_voice_registry.pronunciation import load_pronunciations
from magpie_voice_registry.registry import RegistryError


def test_loads_and_encodes_riva_dictionary(tmp_path: Path) -> None:
    path = tmp_path / "pronunciations.json"
    path.write_text(json.dumps({"Aoi": "aʊi", "Nyla": "naɪlə"}))
    dictionary = load_pronunciations(path)
    assert dictionary.riva_value == "Aoi  aʊi,Nyla  naɪlə"
    assert dictionary.count == 2
    assert dictionary.sha256 == hashlib.sha256(path.read_bytes()).hexdigest()


@pytest.mark.parametrize(
    "payload",
    [{}, {"Aoi": ""}, {"Aoi,": "aʊi"}, {"Aoi": "aʊi,naɪlə"}],
)
def test_rejects_unsafe_or_empty_dictionary(tmp_path: Path, payload: dict) -> None:
    path = tmp_path / "pronunciations.json"
    path.write_text(json.dumps(payload))
    with pytest.raises(RegistryError):
        load_pronunciations(path)
