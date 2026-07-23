# voicebook-stream — runtime qualification spec (PROPOSED)

Status: proposed, awaiting coordinator (Yua) go. Scope: prove the built image
*runs* correctly side-by-side with production, on the real GPU, against the real
backend and real masters. This is the RUNTIME step after the accepted build
checkpoint. **Deploy / cutover / LiveKit wiring / firewall are OUT of scope.**

Artifact under test: `voicebook-stream` image, immutable ID
`sha256:42ae9d3a0d482f9f430adb74aaa081816bcec747abe59f7cd11ae8fabe7d9ab9`
(pin by DIGEST, never the mutable tag).

Every check is routed through `aoi-verify --expect <sentinel> -- <cmd>` so a
check that examined nothing cannot read green.

## Preflight loadout (verified 2026-07-23, read-only)

| Owner | Process | VRAM |
|---|---|---|
| voicebook-tts (prod) | python (pid 551538) | 5774 MiB |
| parakeet-ctl (prod) | tritonserver (pid 640913) | 3630 MiB |
| **free** | — | **6421 MiB** |

GPU: RTX 5060 Ti, 16311 MiB total. No stopped/ghost containers; no stray GPU
processes. faster-qwen needs ~5000 MiB → projected free after load ~1.4 GB.

## Production-safety (continuous, whole run)

- **Untouchable:** voicebook-tts (5055), parakeet-ctl, momo, the shared
  `voicebook-hf-cache` volume (mounted RO), `/srv/voicebook` (RO).
- **Hard STOP boundary:** if free VRAM drops below **800 MiB**, OR prod
  voicebook-tts `/healthz` or parakeet health flaps → `docker rm -f
  voicebook-stream-qual` immediately, then diagnose. The qual dies, prod lives.
- A VRAM sampler runs alongside every generation test.

## Step 0 — start the qual container (pinned by digest, all mounts RO)

```sh
docker run -d --name voicebook-stream-qual --gpus all \
  -v voicebook-hf-cache:/models/hf-cache:ro \
  -v /srv/voicebook:/srv/voicebook:ro \
  -v /srv/voicebook/registry.json:/etc/voicebook/registry.json:ro \
  -e HF_HUB_OFFLINE=1 \
  -e VOICEBOOK_MODEL=/models/hf-cache/hub/models--Qwen--Qwen3-TTS-12Hz-1.7B-Base/snapshots/fd4b254389122332181a7c3db7f27e918eec64e3 \
  -e VOICEBOOK_REGISTRY=/etc/voicebook/registry.json \
  -e VOICEBOOK_HOST=0.0.0.0 -e VOICEBOOK_PORT=5060 \
  -p 127.0.0.1:5060:5060 \
  voicebook-stream@sha256:42ae9d3a0d482f9f430adb74aaa081816bcec747abe59f7cd11ae8fabe7d9ab9
```

## Tests

| # | Test | Command (essence) | PASS criterion | Evidence | Red-proof |
|---|---|---|---|---|---|
| T1 | Startup: hash-verify → offline model load → CUDA-graph warmup | container logs | log shows `registry OK`, model resident, warmup done; no network fetch | literal logs + aoi-verify sentinel | hash-verify-before-load is unit-tested; here confirm it *ran* (log line present) |
| T2 | Health readiness 503→200 (fail-closed) | `curl /healthz` during warm, then after | 503 while warming, 200 only after warmup | two timestamped curls | hit /healthz inside the warm window; must be 503, never 200-while-warming |
| T3 | Unary `/speak` (Hermes) — Sumi + Nyla | `POST /speak {voice_id,text}` | 200, valid non-empty WAV, sample_rate 24000 | saved .wav + header + `wave` duration | unknown voice→404; over-limit→413 (never truncate); empty→502 |
| T4 | Streaming `/speak/stream` (LiveKit) — Sumi + Nyla | `POST /speak/stream` | 200, s16le PCM chunks, headers X-Audio-Format=s16le / X-Sample-Rate=24000 / X-Channels=1; concatenation is valid audio | captured bytes+headers → wav; TTFA measured | empty-stream→502 (not silent 200); prefetched first chunk present, order preserved |
| T5 | Concurrency one-flight (429) | two parallel `/speak` | exactly one 200, one 429; a THIRD after both finish → 200 (lease released) | three status codes | the third-request-200 IS the release red-proof |
| T6 | Cancellation frees the lease | start `/speak/stream`, abort mid-stream, then a fresh request | fresh request → 200 (no leaked lease); server log `outcome=disconnect` + reservation released | curl `--max-time`, follow-up 200, log line | the disconnect→release path (ASGI 2.3 branch) shown in the correlation log |
| T7 | Offline restart | restart container with no network | reaches /healthz 200 with `--network none` | restart + curl 200 | proves no hidden network dependency (HF_HUB_OFFLINE) |
| T8 | VRAM steady-state + TTFA/RTF | nvidia-smi samples during T3/T4 | free VRAM stays > 800 MiB; TTFA & realtime-factor within the earlier envelope (TTFA 168–339 ms, 1.58–2.27× RT) | nvidia-smi samples + stream timing | — |
| T9 | Eric's ear (final gate) | render Sumi + Nyla samples | Eric's judgment on the rendered voices | .wav files for listening | my read is provisional; the pass/fail is his ear |

## Exit

- Qualification PASSES only when T1–T8 are VERIFIED with literal receipts AND T9
  passes Eric's ear. Any single fail = qualification not passed; report which,
  with evidence.
- On completion (pass or fail): `docker rm -f voicebook-stream-qual`. The image
  and prod are unchanged. Deploy/cutover is a separately scoped step after this.
