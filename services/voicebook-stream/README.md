# voicebook-stream

Streaming TTS that speaks a girl's **accepted voicebook master** as new text,
low-latency, in her real voice. Backed by **faster-qwen3-tts** (CUDA-graph
accelerated Qwen3-TTS) — the same weights and masters as the async voicebook,
with a streaming serving path.

    POST /speak/stream  {"voice_id","text"} -> raw s16le PCM stream  (LiveKit)
    POST /speak         {"voice_id","text"} -> completed audio/wav   (Hermes)
    GET  /healthz       -> 503 while warming, 200 only after CUDA-graph warmup

## Why it replaces voicebook-tts

voicebook-tts returns a completed WAV only, so time-to-first-audio equals full
synthesis (~1.6x slower than realtime). This service streams the first PCM
chunk in ~225ms and generates faster than realtime, which is what a live call
needs — while still serving completed WAVs for Hermes. One model, both paths.

## Rules the code enforces (each earned by a review cycle)

| rule | why |
|---|---|
| client sends `voice_id` only | a client path/hash makes the identity guarantee decorative |
| master hash re-verified before every use | a swapped file must never be spoken |
| one generation in flight, acquired synchronously | collision is a clean 429, not a half-sent 200 |
| reservation released in the response finally | covers disconnect, error, and completion — even an unstarted body |
| first chunk prefetched; empty -> 502 | a 200 with silence is the silent-truncation class |
| `s16le` wire, `application/octet-stream` + `X-Audio-Format` | audio/L16 is big-endian (RFC 2586); mislabelling is a false contract |
| health 503 until warmup | a warming service must not read healthy |
| one correlation record per request | request_id, voice_id, chars, outcome, duration — never text |

## Verify

    uv run pytest services/voicebook-stream/tests   # no GPU
    make lint && make typecheck
