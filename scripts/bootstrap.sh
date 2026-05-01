#!/usr/bin/env bash
#
# First-time machine setup. Safe to re-run — every step checks before
# installing. Does NOT deploy agents or register SIP routing; those are
# separate explicit steps so you can review before running them.
#
# Run once on a fresh box:
#   scripts/bootstrap.sh
#
# Then:
#   1. Fill in secrets/livekit-agents.env
#   2. Edit config/*.yaml and config/*.json for real values
#   3. Bring up infra:     make up
#   4. Register SIP:       make register-sip
#   5. Deploy agents:      make deploy
#   6. Verify:             make health

set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

log()  { printf "\033[1;34m[bootstrap]\033[0m %s\n" "$*"; }
warn() { printf "\033[1;33m[warn]\033[0m      %s\n" "$*"; }
die()  { printf "\033[1;31m[fatal]\033[0m     %s\n" "$*" >&2; exit 1; }

# ---- required tooling --------------------------------------------
command -v brew   >/dev/null 2>&1 || die "Homebrew required: https://brew.sh"
command -v docker >/dev/null 2>&1 || die "Docker Desktop required: https://docker.com"

for tool in uv livekit-cli jq; do
  if brew list "$tool" >/dev/null 2>&1; then
    log "$tool: already installed"
  else
    log "installing $tool via brew"
    brew install "$tool"
  fi
done

# ---- in-repo directory scaffolding -------------------------------
mkdir -p "${REPO_ROOT}/secrets" \
         "${REPO_ROOT}/logs/voice"
chmod 700 "${REPO_ROOT}/secrets"

# Copy each .example into place if the real file doesn't exist yet.
# Real files are gitignored via .gitignore (secrets/, config/*.yaml,
# config/sip-*.json).
copy_if_missing() {
  local src="$1" dst="$2"
  if [[ -f "$dst" ]]; then
    log "$(basename "$dst"): already present — leaving untouched"
  else
    cp "$src" "$dst"
    log "copied template: $(basename "$dst")"
    warn "EDIT ${dst} before using"
  fi
}

copy_if_missing "${REPO_ROOT}/config/livekit.yaml.example"           "${REPO_ROOT}/config/livekit.yaml"
copy_if_missing "${REPO_ROOT}/config/livekit-sip.yaml.example"       "${REPO_ROOT}/config/livekit-sip.yaml"
copy_if_missing "${REPO_ROOT}/config/livekit-egress.yaml.example"    "${REPO_ROOT}/config/livekit-egress.yaml"
copy_if_missing "${REPO_ROOT}/config/sip-inbound-trunk.json.example" "${REPO_ROOT}/config/sip-inbound-trunk.json"
for a in nyla aoi party; do
  copy_if_missing "${REPO_ROOT}/config/sip-dispatch-${a}.json.example" "${REPO_ROOT}/config/sip-dispatch-${a}.json"
done
copy_if_missing "${REPO_ROOT}/config/secrets.env.example"            "${REPO_ROOT}/secrets/livekit-agents.env"

# ---- root workspace venv ------------------------------------------
# One venv at the repo root serves every workspace member (sdk, tools,
# agents/*). `uv sync` wires them all up as editable installs from the
# workspace manifest in the root pyproject.toml.
if [[ -x "${REPO_ROOT}/.venv/bin/python" ]]; then
  log "venv: root workspace already synced"
else
  log "venv: uv sync --all-groups (root workspace)"
  (cd "${REPO_ROOT}" && uv sync --all-groups)
fi

log "done."
cat <<EOF

Next steps:

  1. Edit ${REPO_ROOT}/secrets/livekit-agents.env
     (GOOGLE_API_KEY, GATEWAY_AUTH_TOKEN, LIVEKIT_API_SECRET,
      DISCORD_TOKEN_NYLA, DISCORD_TOKEN_AOI)

  2. Edit ${REPO_ROOT}/config/livekit.yaml
     (keys section: set a real api_secret)

  3. Edit ${REPO_ROOT}/config/livekit-sip.yaml and livekit-egress.yaml
     (api_key/api_secret: match livekit.yaml)

  4. Edit ${REPO_ROOT}/config/sip-inbound-trunk.json
     (numbers: your Twilio DIDs; allowed_numbers: caller allowlist)

  5. Edit ${REPO_ROOT}/config/sip-dispatch-{nyla,aoi,party}.json
     (numbers: the DID each agent owns)

  6. brew services stop redis      # compose ships redis on :6379
     make up                        # brings up redis, livekit-server, livekit-sip
     make register-sip
     make deploy
     make health

EOF
