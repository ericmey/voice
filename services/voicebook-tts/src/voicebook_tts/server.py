"""Service entrypoint.

Builds the registry from server-side config, verifies every master BEFORE the
model is loaded (fail fast and cheap), then serves.

Binding is private-interface only. This service speaks in the voices of real
people in this house; it has no business on a public address.
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

from .app import create_app
from .registry import RegistryError, VoiceEntry, VoiceRegistry

#: Server-side registry. The client never supplies any of this.
DEFAULT_REGISTRY = Path("/etc/voicebook/registry.json")

#: Private interface only. Overridable, but never defaulted to 0.0.0.0.
DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 5055


def load_registry(path: Path) -> VoiceRegistry:
    """Read the allowlist. Every field is server-owned and mandatory."""
    try:
        raw = json.loads(path.read_text())
    except FileNotFoundError:
        raise RegistryError(f"no registry at {path}") from None
    except json.JSONDecodeError as exc:
        raise RegistryError(f"registry at {path} is not valid JSON: {exc}") from None

    entries: dict[str, VoiceEntry] = {}
    for voice_id, spec in raw.items():
        missing = {"master_path", "reference_transcript", "sha256"} - spec.keys()
        if missing:
            raise RegistryError(f"{voice_id}: registry entry missing {sorted(missing)}")
        entries[voice_id] = VoiceEntry(
            voice_id=voice_id,
            master_path=Path(spec["master_path"]),
            reference_transcript=spec["reference_transcript"],
            expected_sha256=spec["sha256"],
        )
    return VoiceRegistry(entries)


def main() -> int:
    import uvicorn

    registry_path = Path(os.environ.get("VOICEBOOK_REGISTRY", DEFAULT_REGISTRY))
    host = os.environ.get("VOICEBOOK_HOST", DEFAULT_HOST)
    port = int(os.environ.get("VOICEBOOK_PORT", DEFAULT_PORT))
    model_id = os.environ.get("VOICEBOOK_MODEL", "Qwen/Qwen3-TTS-12Hz-1.7B-Base")

    try:
        registry = load_registry(registry_path)
        # Verify masters BEFORE spending ~5s loading a model we may not be
        # allowed to use. A bad hash should cost a second, not a minute.
        registry.verify_all()
    except RegistryError as exc:
        print(f"voicebook-tts: FATAL {exc}", file=sys.stderr)
        return 2

    print(f"voicebook-tts: registry OK — {registry.voice_ids}", flush=True)

    from .synth import QwenBaseSynthesizer

    synthesizer = QwenBaseSynthesizer(model_id)
    print(f"voicebook-tts: model resident — {model_id}", flush=True)

    uvicorn.run(create_app(registry, synthesizer), host=host, port=port, log_level="info")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
