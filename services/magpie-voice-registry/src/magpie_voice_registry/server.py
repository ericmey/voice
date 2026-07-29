from __future__ import annotations

import os

import uvicorn

from .app import create_app
from .registry import RegistryError, load_registry


def main() -> None:
    registry_path = os.environ.get("MAGPIE_VOICE_REGISTRY", "/config/registry.json")
    nim_url = os.environ.get("MAGPIE_NIM_URL", "http://10.0.20.25:9101")
    capacity = int(os.environ.get("MAGPIE_VOICE_CAPACITY", "8"))
    try:
        registry = load_registry(registry_path)
    except RegistryError as exc:
        raise SystemExit(f"magpie-voice-registry: FATAL {exc}") from exc
    print(f"magpie-voice-registry: registry OK - {registry.voice_ids}", flush=True)
    uvicorn.run(
        create_app(registry, nim_url, capacity=capacity),
        host=os.environ.get("MAGPIE_REGISTRY_HOST", "0.0.0.0"),
        port=int(os.environ.get("MAGPIE_REGISTRY_PORT", "5056")),
    )


if __name__ == "__main__":
    main()
