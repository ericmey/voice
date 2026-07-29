from __future__ import annotations

import os

import uvicorn

from .aliases import load_speech_aliases
from .app import create_app
from .pronunciation import load_pronunciations
from .registry import RegistryError, load_registry


def main() -> None:
    registry_path = os.environ.get("MAGPIE_VOICE_REGISTRY", "/config/registry.json")
    pronunciations_path = os.environ.get(
        "MAGPIE_PRONUNCIATION_DICTIONARY", "/config/pronunciations.json"
    )
    aliases_path = os.environ.get("MAGPIE_SPEECH_ALIASES", "/config/speech-aliases.json")
    nim_url = os.environ.get("MAGPIE_NIM_URL", "http://10.0.20.25:9101")
    capacity = int(os.environ.get("MAGPIE_VOICE_CAPACITY", "8"))
    try:
        registry = load_registry(registry_path)
        pronunciations = load_pronunciations(pronunciations_path)
        speech_aliases = load_speech_aliases(aliases_path)
        required = {
            voice_id.strip()
            for voice_id in os.environ.get("MAGPIE_REQUIRED_VOICE_IDS", "").split(",")
            if voice_id.strip()
        }
        if required:
            registry.require_exactly(required)
    except RegistryError as exc:
        raise SystemExit(f"magpie-voice-registry: FATAL {exc}") from exc
    print(
        f"magpie-voice-registry: registry OK - {registry.voice_ids}; "
        f"pronunciations={pronunciations.count} sha256={pronunciations.sha256}; "
        f"speech_aliases={speech_aliases.count} sha256={speech_aliases.sha256}",
        flush=True,
    )
    uvicorn.run(
        create_app(
            registry,
            nim_url,
            capacity=capacity,
            pronunciations=pronunciations,
            speech_aliases=speech_aliases,
        ),
        host=os.environ.get("MAGPIE_REGISTRY_HOST", "0.0.0.0"),
        port=int(os.environ.get("MAGPIE_REGISTRY_PORT", "5056")),
    )


if __name__ == "__main__":
    main()
