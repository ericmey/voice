# Magpie voice registry

A narrow named-voice adapter in front of NVIDIA Magpie TTS Zero Shot. Clients
send only a stable `voice_id`; the service owns the prompt path, expected hash,
quality, language, and sample rate.

It intentionally preserves the two fleet contracts used by the former Qwen
Voicebook service:

```text
POST /speak/stream  {"voice_id":"sumi-v1","text":"..."} -> raw s16le PCM
POST /speak         {"voice_id":"sumi-v1","text":"..."} -> audio/wav
GET  /healthz                                               -> readiness
GET  /voices                                                -> public roster
```

This is not a second synthesis stack. It does not load a model or clone a voice
itself; it validates named prompt artifacts and forwards synthesis to the pinned
Magpie NIM. A missing prompt, checksum mismatch, wrong WAV format, unsupported
quality, or unavailable NIM fails loudly. Prompt paths are never accepted from
clients or returned by the API.

## Registry

Copy `registry.example.json` and provide it with `MAGPIE_VOICE_REGISTRY`. Prompt
WAV requirements are enforced at startup: PCM s16, mono, 22.05 kHz or higher,
and 3-10 seconds. Production prompts are peak-normalized to -12 dBFS and use
quality 40.

## Runtime

```bash
docker build -t magpie-voice-registry:0.1.0 services/magpie-voice-registry
docker run --rm -p 5056:5056 \
  -e MAGPIE_NIM_URL=http://10.0.20.25:9101 \
  -e MAGPIE_VOICE_REGISTRY=/config/registry.json \
  -v /home/ericmey/voice/voice-prompts:/prompts:ro \
  -v /home/ericmey/voice/magpie-registry:/config:ro \
  magpie-voice-registry:0.1.0
```
