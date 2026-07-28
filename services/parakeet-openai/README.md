# parakeet-openai

OpenAI-shaped transcription in front of our NVIDIA Riva / Parakeet NIM.

```
POST /v1/audio/transcriptions   multipart -> {"text": "..."}
GET  /healthz                   503 until the gRPC backend answers
```

## Why this exists

Parakeet publishes its own `/v1/audio/transcriptions`. Ours does not work, and
the reason matters:

```
$ curl -F file=@clip.wav http://10.0.20.25:9000/v1/audio/transcriptions
{"detail":"Model not found for language en"}
```

The deployed profile is `mode=str` — streaming only. That route needs an
**offline** model registered, and none is. The `mode=all` profile exists but is
marked incompatible for our card and would want roughly 6–7 GB against the
3.6 GB the streaming profile uses — VRAM we do not have spare on GPU1 beside
llama-server. Redeploying to `mode=all` is a real project with a real capacity
question behind it.

Meanwhile the streaming gRPC contract works, and works well: **64 concurrent
streams, zero errors, 93× realtime** when measured. Every harness we run
(Hermes, OpenClaw) speaks OpenAI-shaped HTTP. This service is the seam between
those two facts and nothing else. It holds no model and no state, so it restarts
instantly and is obviously not the component that broke.

## Contracts it keeps

These are deliberate and each one is a test:

- **An unusable upload is a typed 4xx.** It is never transcribed best-effort. A
  wrong-sample-rate stream does not error at Riva — it returns fluent,
  confident, *wrong* text, which is strictly worse than a failure.
- **A backend failure is a 502 carrying the real reason**, never an empty
  transcript. `""` reads to a caller as "silence", i.e. as success.
- **An empty transcript at 200 means genuine silence** — reachable only on a
  successful recognition, because every failure took the 502 branch first.
- **Readiness is measured against the backend**, not against this process being
  up. A shim reporting healthy while its backend is down is the exact failure
  shape this fleet keeps finding.
- **Anything we cannot honour is refused**, not silently substituted.

## Audio normalization

Riva wants 16 kHz mono s16le. Two paths:

- **fast path** — the upload is already RIFF/WAVE at exactly that shape, so its
  frames go straight through with no external process.
- **everything else** — `ffmpeg`. Not `audioop` (deprecated in 3.12, **removed
  in 3.13** — building on it would put a fixed expiry date on the service), and
  not a hand-rolled resampler (correct resampling is real signal processing;
  this is a seam, not a DSP project).

`ffmpeg` is therefore a hard dependency and the Docker build fails without it.

## Configuration

| env | default | meaning |
| --- | --- | --- |
| `PARAKEET_URI` | `parakeet-ctl:50051` | Riva gRPC endpoint |
| `PARAKEET_LANGUAGE` | `en-US` | recognition language |
| `PARAKEET_PUNCTUATE` | `true` | automatic punctuation |
| `PARAKEET_OPENAI_HOST` | `0.0.0.0` | LAN-bound **by design** |
| `PARAKEET_OPENAI_PORT` | `5057` | |
| `PARAKEET_MAX_UPLOAD_BYTES` | `26214400` (25 MB) | refused over, never truncated |

The bind is LAN, not loopback, deliberately: nyla.mey.house, hana.mey.house and
the command chair all need this. A loopback bind makes the service look healthy
on mizuki and be invisible to every caller that needs it — that exact fault cost
us Nyla's 5pm brief on 2026-07-28.

Like the rest of the server-VLAN voice stack it is unauthenticated, which Eric
accepted explicitly for internal deployment.

## Client shape

Any OpenAI SDK, pointed here:

```python
from openai import OpenAI
client = OpenAI(base_url="http://10.0.20.25:5057/v1", api_key="not-required")
client.audio.transcriptions.create(model="parakeet", file=open("clip.wav", "rb"))
```

`model` is accepted as compatibility metadata and logged, never dispatched on —
SDKs require the field and this service has exactly one engine.
`response_format` supports `json`, `text`, and `verbose_json`.

## Tests

```sh
uv venv .venv --python 3.11
uv pip install --python .venv/bin/python "fastapi>=0.115" "uvicorn[standard]>=0.30" \
    "python-multipart>=0.0.9" pytest httpx
PYTHONPATH=src .venv/bin/python -m pytest tests -q
```

The unit tests inject a fake backend and need neither the Riva SDK nor a GPU.
The live end-to-end proof — real TTS audio through the real gRPC path — is
recorded in `RECEIPTS.md`.
