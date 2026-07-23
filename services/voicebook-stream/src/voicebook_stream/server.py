"""Service entrypoint for voicebook-stream.

Startup order is load-bearing:
  1. Load the server-owned registry (voice_id -> master path + transcript + hash).
  2. Verify EVERY master hash BEFORE loading the ~5s model — a bad hash should
     cost a second, not a minute, and must never reach synthesis.
  3. Construct the faster-qwen backend (loads weights, captures CUDA graphs).
  4. WARM UP against a registry master so /healthz stays 503 until the graph is
     captured. Readiness is fail-closed.
  5. Bind the PRIVATE interface only and serve.

GPU imports live inside main(), so the API/registry/lease/route tests run with
no CUDA present.
"""

from __future__ import annotations

import json
import logging
import os
import sys
from pathlib import Path

from .app import create_app
from .lease import OneFlightLease
from .registry import RegistryError, VoiceEntry, VoiceRegistry

#: Server-owned registry. The client never supplies any of this.
DEFAULT_REGISTRY = Path("/etc/voicebook/registry.json")
#: Private interface only. Never defaulted to 0.0.0.0.
DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 5060  # distinct from voicebook-tts (5055), runs side-by-side


def load_registry(path: Path) -> VoiceRegistry:
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
    logging.basicConfig(level=logging.INFO, format="%(levelname)s:     voicebook %(message)s")
    import uvicorn

    registry_path = Path(os.environ.get("VOICEBOOK_REGISTRY", DEFAULT_REGISTRY))
    host = os.environ.get("VOICEBOOK_HOST", DEFAULT_HOST)
    port = int(os.environ.get("VOICEBOOK_PORT", DEFAULT_PORT))
    model_path = os.environ.get("VOICEBOOK_MODEL")
    if not model_path:
        print("voicebook-stream: FATAL VOICEBOOK_MODEL (snapshot path) is required", file=sys.stderr)
        return 2

    try:
        registry = load_registry(registry_path)
        registry.verify_all()  # verify masters BEFORE loading the model
    except RegistryError as exc:
        print(f"voicebook-stream: FATAL {exc}", file=sys.stderr)
        return 2
    print(f"voicebook-stream: registry OK — {registry.voice_ids}", flush=True)

    from .synth import StreamingSynthesizer

    synthesizer = StreamingSynthesizer(model_path)
    print("voicebook-stream: model resident; warming CUDA graph…", flush=True)
    # Warm against the first registry master so readiness reflects a captured graph.
    warm_id = registry.voice_ids[0]
    warm = registry.get(warm_id)
    synthesizer.warmup(warm.master_path, warm.reference_transcript)
    print(f"voicebook-stream: warm (via {warm_id}); ready", flush=True)

    uvicorn.run(
        create_app(registry, synthesizer, OneFlightLease()),
        host=host,
        port=port,
        log_level="info",
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
