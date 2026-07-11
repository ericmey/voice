LIVEKIT_API_KEY=op://Harem World/mizuki-livekit-api/username
LIVEKIT_API_SECRET=op://Harem World/mizuki-livekit-api/credential
GOOGLE_API_KEY=op://Harem World/google-gemini-api/credential

# Per-agent Musubi bearers. ONE PER AGENT — never share one.
#
# `scripts/agent-entrypoint.sh` selects the suffixed var matching $AGENT and exports it as
# the unsuffixed MUSUBI_V2_TOKEN, which is what the SDK reads. A missing entry here is not
# a cosmetic gap: re-render this template and that agent crash-loops (exit 78), and the
# likely operator "fix" is pasting a sibling's bearer — which silently writes her memories
# into someone else's namespace. Every agent that exists must have a line here.
#
# SUMI: her voice line is `musubi-v2-sumi-voice`, NOT `musubi-v2-sumi`. The latter is her
# FLEET presence (sumi/hermes); the voice line writes to the distinct `sumi/voice`
# namespace and carries its own bearer. Verified 2026-07-11 by hashing the live token in
# the running container against both 1Password items — guessing the obvious name would
# have pointed her voice memories at the wrong plane.
MUSUBI_V2_TOKEN_AOI=op://Harem World/musubi-v2-aoi/credential
MUSUBI_V2_TOKEN_NYLA=op://Harem World/musubi-v2-nyla/credential
MUSUBI_V2_TOKEN_YUA=op://Harem World/musubi-v2-yua/credential
MUSUBI_V2_TOKEN_SUMI=op://Harem World/musubi-v2-sumi-voice/credential
MUSUBI_V2_BASE_URL=http://musubi.mey.house:8100/v1

# REMOVED 2026-07-11 — provisioned live credentials that nothing read:
#
#   OPENAI_API_KEY     — read by NO code. Sumi's only OpenAI-plugin call passes an explicit
#                        `api_key="sk-local"` for the local Nemo endpoint
#                        (agents/sumi/src/agent.py); aoi/nyla/yua are livekit-agents[google]
#                        and never touch it. It was being handed to all four containers for
#                        nothing — a live key distributed without a consumer.
#   ELEVENLABS_API_KEY — the ElevenLabs plugin is not a dependency of any agent. Only an
#                        env alias in sdk/env.py referenced it, now also removed.
#
# Least privilege: a container gets the credentials it uses, and no others.

# Observability. The collector lives on shiori (otel-collector -> Tempo/Loki/Mimir);
# agents export to it directly over OTLP/HTTP. Metrics and logs fall back to
# VOICE_OTLP_ENDPOINT's host when their own endpoint is unset, but naming all
# three keeps the intent visible.
VOICE_OTEL_ENABLED=true
VOICE_OTEL_LOGS_ENABLED=true
VOICE_OTEL_METRICS_ENABLED=true
VOICE_OTLP_ENDPOINT=http://shiori.mey.house:4318/v1/traces
VOICE_OTLP_LOGS_ENDPOINT=http://shiori.mey.house:4318/v1/logs
VOICE_OTLP_METRICS_ENDPOINT=http://shiori.mey.house:4318/v1/metrics
VOICE_DEPLOYMENT_ENVIRONMENT=production
