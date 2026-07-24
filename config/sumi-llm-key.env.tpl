# Least-privilege injection surface for Sumi LLM key provisioning + worker launch.
# Resolves ONLY the Sumi LiteLLM bearer — NOT the full livekit.env.tpl (which pulls eight
# 1Password refs: LiveKit key/secret, OpenAI, Google, ElevenLabs, and Aoi/Nyla/Yua Musubi
# tokens). Using this template means op-run injects one secret into the verifier/CLI process,
# not eight unrelated ones. (D1, Yua's v7 second-read.)
SUMI_LLM_API_KEY=op://Harem World/sumi-voice-litellm-api-key/credential
