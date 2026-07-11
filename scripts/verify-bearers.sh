#!/usr/bin/env bash
#
# Prove each agent's Musubi bearer IS that agent, before the containers come up.
# `make deploy` and `make cycle` hard-depend on this. It must therefore run from ANY shell —
# ssh one-shot, cron, CI runner — not only from an interactive login.
#
# It did not. It called `uv` bare, and a non-interactive shell has no ~/.local/bin on PATH,
# so the gate guarding the deploy could not itself run. (Yua, round 6.)
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

# shellcheck source=scripts/lib/tool-path.sh
source "${REPO_ROOT}/scripts/lib/tool-path.sh"
ensure_tool uv

SECRETS_ENV="${1:-${VOICE_SECRETS_ENV:-${REPO_ROOT}/secrets/livekit-agents.env}}"

if [[ ! -r "${SECRETS_ENV}" ]]; then
  printf '\033[1;31m[fatal]\033[0m no rendered secrets file at %s\n' "${SECRETS_ENV}" >&2
  printf '        render it from config/livekit.env.tpl before deploying.\n' >&2
  exit 78
fi

cd "${REPO_ROOT}"
exec uv run python -m sdk.bearer_identity "${SECRETS_ENV}"
