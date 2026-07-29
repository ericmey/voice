# Magpie voice registry

A narrow named-voice adapter in front of NVIDIA Magpie TTS Zero Shot. Clients
send only a stable `voice_id`; the service owns the prompt path, expected hash,
quality, language, and sample rate.

It intentionally preserves the two fleet contracts used by the former Qwen
Voicebook service:

```text
POST /speak/stream  {"voice_id":"sumi-v1","text":"..."} -> raw s16le PCM
POST /speak         {"voice_id":"sumi-v1","text":"..."} -> audio/wav
POST /v1/audio/speech {"voice":"sumi-v1","input":"..."} -> audio/wav
GET  /healthz                                               -> readiness
GET  /voices                                                -> public roster
```

The OpenAI-compatible route is a translation layer over `/speak`, not a second
synthesis implementation. It accepts completed WAV at speed `1.0`; unsupported
formats or speeds return HTTP 400.

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

Set `MAGPIE_REQUIRED_VOICE_IDS` to a comma-separated production roster. Startup
requires an exact match: both missing and unexpected IDs are fatal. This keeps
an accidentally partial or stale registry from presenting itself as ready.

Set `MAGPIE_PRONUNCIATION_DICTIONARY` to a JSON object mapping written forms to
IPA forms, for example `{"Aoi":"aʊi"}`. The service validates it once at
startup and sends the resulting Riva `custom_dictionary` on every synthesis
request. `/healthz` exposes its entry count and SHA-256 so operators can prove
which dictionary is live without exposing prompt paths.

Set `MAGPIE_SPEECH_ALIASES` to a JSON object mapping canonical written terms to
plain-language spoken replacements. Aliases are case-insensitive, use whole
terms only, and apply longest keys first before synthesis. Keep semantic aliases
separate from the IPA dictionary: they intentionally change what is spoken,
while pronunciation entries change how the same word is pronounced. Health
reports alias count and SHA-256 separately.

## Runtime

```bash
docker build -t magpie-voice-registry:0.1.0 services/magpie-voice-registry
docker run --rm -p 5056:5056 \
  -e MAGPIE_NIM_URL=http://10.0.20.25:9101 \
  -e MAGPIE_VOICE_REGISTRY=/config/registry.json \
  -e MAGPIE_PRONUNCIATION_DICTIONARY=/config/pronunciations.json \
  -e MAGPIE_SPEECH_ALIASES=/config/speech-aliases.json \
  -v /home/ericmey/voice/voice-prompts:/prompts:ro \
  -v /home/ericmey/voice/magpie-registry/registry.json:/config/registry.json:ro \
  -v /home/ericmey/voice/deploy/magpie/pronunciations.production.json:/config/pronunciations.json:ro \
  -v /home/ericmey/voice/deploy/magpie/speech-aliases.production.json:/config/speech-aliases.json:ro \
  magpie-voice-registry:0.1.0
```
