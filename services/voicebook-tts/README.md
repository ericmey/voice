# voicebook-tts

Persistent Qwen Base service. Speaks a girl's **accepted voicebook master** as
new text, without re-authoring her.

    POST /speak   {"voice_id": "nyla-v1", "text": "..."}  -> audio/wav
    GET  /voices                                          -> allowlisted ids
    GET  /healthz                                         -> liveness + registry

## Why it exists

Voice **authoring** (Qwen VoiceDesign) creates a woman once. Voice **cloning**
(Qwen Base) makes that saved woman say anything afterwards. We never re-author —
a second design run produces a different person who happens to share a prompt.

Model load costs ~5s, so this is a resident service rather than a CLI call.

## Rules the code enforces

| rule | why |
|---|---|
| client sends `voice_id` only | a client-supplied path or hash makes the identity guarantee decorative |
| masters re-verified by SHA-256 **before every generation** | startup verification is not a permanent promise; a resident process would happily serve a swapped file |
| over-limit input → typed **413**, never truncated | a silently shortened daily summary reads as success |
| one generation in flight → **429** | concurrency is unproven; refuse rather than queue invisibly |
| binds `127.0.0.1` by default | these are real people's voices |
| GPU deps are **image-only** | keeps CUDA torch out of every root `uv sync` |

Masters are **read-only inputs**. This service never mutates canon.

## Registry

Server-owned, `/etc/voicebook/registry.json`:

```json
{
  "nyla-v1": {
    "master_path": "/srv/voicebook/nyla/nyla-voice-master-v1.wav",
    "reference_transcript": "...exact words spoken in the master...",
    "sha256": "3dcc3f1d..."
  }
}
```

## Verify

    uv run pytest services/voicebook-tts/tests   # no GPU required
    make lint && make typecheck

The suite is red-proofed: disabling the hash comparison in `registry.py` must
make `test_swapped_master_is_detected_before_speaking` fail.
