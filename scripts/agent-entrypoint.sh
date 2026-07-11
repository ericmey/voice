#!/bin/sh
# Resolve per-agent secrets into the unsuffixed names the SDK reads, then exec
# the agent.
#
# secrets/livekit-agents.env carries MUSUBI_V2_TOKEN_{AOI,NYLA,YUA}; the SDK
# reads MUSUBI_V2_TOKEN. The retired launchd deploy resolved that per agent
# while rendering each plist. Compose cannot: ${VAR} inside an `environment:`
# block interpolates from the shell and .env, never from env_file. So the
# selection happens here, once, at container start.
set -eu

: "${AGENT:?AGENT must be set (aoi|nyla|yua|sumi)}"

case "$AGENT" in
	aoi) token_var=MUSUBI_V2_TOKEN_AOI ;;
	yua) token_var=MUSUBI_V2_TOKEN_YUA ;;
	nyla) token_var=MUSUBI_V2_TOKEN_NYLA ;;
	# Sumi's voice line writes to her own sumi/voice namespace, a distinct
	# channel from her fleet presence (sumi/hermes), so it carries its own
	# bearer.
	sumi) token_var=MUSUBI_V2_TOKEN_SUMI ;;
	*)
		echo "agent-entrypoint: no Musubi token mapping for AGENT=$AGENT" >&2
		exit 64
		;;
esac

# Indirect expansion, so the error below names the variable that is actually
# required for this agent.
eval "musubi_token=\${${token_var}:-}"

# Refuse to start on an empty bearer. Musubi answers 401 "missing bearer token",
# and the tool layer degrades that into a friendly "memory is unavailable right
# now" line the agent says out loud — so the call sounds healthy while nothing
# is ever recalled or stored. Crash-looping is the honest failure.
if [ -z "$musubi_token" ]; then
	echo "agent-entrypoint: $token_var is empty (required for AGENT=$AGENT)" >&2
	exit 78
fi
export MUSUBI_V2_TOKEN="$musubi_token"

# Transcripts, trace, telemetry, post-call review and post-call memory all
# resolve their paths from this one variable. Unset, each silently no-ops.
export LIVEKIT_VOICE_LOGS="${LIVEKIT_VOICE_LOGS:-/app/logs/voice}"

# tracing.py builds SERVICE_NAME as voice-<VOICE_AGENT_NAME>, falling back to a
# bare "voice" when unset. Unset, all four agents report as one service and every
# per-agent dashboard panel and alert selector (`voice-.*`) matches nothing.
export VOICE_AGENT_NAME="$AGENT"

exec uv run python "agents/${AGENT}/src/agent.py" start
