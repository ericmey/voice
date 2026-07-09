LIVEKIT_API_KEY=op://Harem World/mizuki-livekit-api/username
LIVEKIT_API_SECRET=op://Harem World/mizuki-livekit-api/credential
OPENAI_API_KEY=op://Harem World/openai-api/credential
GOOGLE_API_KEY=op://Harem World/google-gemini-api/credential
ELEVENLABS_API_KEY=op://Harem World/elevenlabs-api/credential
DISCORD_TOKEN_AOI=op://Harem World/discord-bot-aoi/credential
DISCORD_TOKEN_NYLA=op://Harem World/discord-bot-nyla/credential
DISCORD_TOKEN_YUA=op://Harem World/discord-bot-yua/credential
MUSUBI_V2_TOKEN_AOI=op://Harem World/musubi-v2-aoi/credential
MUSUBI_V2_TOKEN_NYLA=op://Harem World/musubi-v2-nyla/credential
MUSUBI_V2_TOKEN_YUA=op://Harem World/musubi-v2-yua/credential
MUSUBI_V2_BASE_URL=http://musubi.mey.house:8100/v1

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
