# LEGACY LITELLM-ONLY TEMPLATE. The production Sumi worker has called the local
# llama.cpp service directly since 2026-07-26 and does not use this 1Password
# item. Keep this only for a deliberate future return to the LiteLLM route; do
# not run the provisioner to repair the current direct route.
#
# Least-privilege injection surface for Sumi LLM key provisioning + worker launch.
# Resolves ONLY the Sumi LiteLLM bearer — NOT the full livekit.env.tpl (which pulls eight
# 1Password refs: LiveKit key/secret, OpenAI, Google, ElevenLabs, and Aoi/Nyla/Yua Musubi
# tokens). Using this template means op-run injects one secret into the verifier/CLI process,
# not eight unrelated ones. (D1, Yua's v7 second-read.)
SUMI_LLM_API_KEY=op://Harem World/sumi-voice-litellm-api-key/credential
