#!/usr/bin/env bash
#
# Shared environment bootstrap for operator scripts that call LiveKit services.
#
# Launchd and non-login SSH shells often do not have Homebrew on PATH, and the
# SIP CLI falls back to dev credentials when LIVEKIT_API_KEY/SECRET are absent.
# Re-exec through the repo's 1Password env template so bare `make health` and
# `make register-sip` behave like the deployed service.

livekit_prepend_operator_path() {
  export PATH="/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin:${PATH:-}"
}

livekit_source_connect_env_if_needed() {
  if [[ -n "${OP_CONNECT_HOST:-}" && -n "${OP_CONNECT_TOKEN:-}" ]]; then
    return 0
  fi

  local service_env="${VOICE_SERVICE_ENV:-${HOME}/.voice/service-env/ai.voice.env}"
  if [[ -r "$service_env" ]]; then
    set -a
    # shellcheck disable=SC1090
    source "$service_env"
    set +a
  fi
}

livekit_reexec_with_1password_if_needed() {
  local repo_root="$1"
  shift

  livekit_prepend_operator_path

  if [[ -n "${LIVEKIT_API_KEY:-}" && -n "${LIVEKIT_API_SECRET:-}" ]]; then
    return 0
  fi

  if [[ "${VOICE_LIVEKIT_ENV_BOOTSTRAPPED:-}" == "1" ]]; then
    printf '[fatal] LIVEKIT_API_KEY/LIVEKIT_API_SECRET missing after 1Password env bootstrap\n' >&2
    exit 1
  fi

  local env_template="${LIVEKIT_ENV_TEMPLATE:-${repo_root}/config/livekit.env.tpl}"
  if [[ ! -r "$env_template" ]]; then
    printf '[fatal] LiveKit env template not readable: %s\n' "$env_template" >&2
    exit 1
  fi

  command -v op >/dev/null 2>&1 || {
    printf '[fatal] 1Password CLI `op` not found on PATH; expected /opt/homebrew/bin/op or equivalent\n' >&2
    exit 1
  }

  livekit_source_connect_env_if_needed
  if [[ -z "${OP_CONNECT_HOST:-}" || -z "${OP_CONNECT_TOKEN:-}" ]]; then
    printf '[fatal] OP_CONNECT_HOST/OP_CONNECT_TOKEN missing; cannot load %s\n' "$env_template" >&2
    exit 1
  fi

  export VOICE_LIVEKIT_ENV_BOOTSTRAPPED=1
  exec op run --env-file="$env_template" -- "$@"
}
