#!/usr/bin/env bash
#
# Shared environment bootstrap for operator scripts that call LiveKit services.
#
# Non-login SSH shells often have a minimal PATH, and the SIP CLI falls back to dev
# credentials when LIVEKIT_API_KEY/SECRET are absent. Re-exec through the repo's 1Password
# env template so bare `make health` and `make register-sip` behave like the deployed service.
#
# The launchd/Homebrew assumptions here were fossils from the RETIRED macOS deploy. The stack
# runs on mizuki (Ubuntu) under docker compose. Verified on the host 2026-07-11:
# /opt/homebrew does not exist, and `op` is at /usr/bin/op — so the old PATH prepend put a
# nonexistent directory first and the old error message named a path that could never be
# right. Misleading at 2am is the whole cost of a fossil.
#
# Homebrew paths are still included when they EXIST, so this keeps working from a macOS dev
# laptop. Present-if-real, never assumed.

livekit_prepend_operator_path() {
  local p="/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin"
  [[ -d /opt/homebrew/bin ]] && p="/opt/homebrew/bin:$p"   # macOS dev laptop, if present
  export PATH="$p:${PATH:-}"
}

livekit_source_connect_env_if_needed() {
  if [[ -n "${OP_CONNECT_HOST:-}" && -n "${OP_CONNECT_TOKEN:-}" ]]; then
    return 0
  fi

  # VOICE_SERVICE_ENV used to default to ~/.voice/service-env/ai.voice.env — a path from the
  # RETIRED launchd deploy. It does not exist on mizuki (verified 2026-07-11), so this
  # function silently did nothing and the caller carried on none the wiser. Another quiet
  # fossil: not breaking anything today, and unreadable the day it matters.
  #
  # There is no default now. If you want a 1Password Connect env sourced, name it. If you
  # name one that is not readable, you are told — because a config file you meant to load and
  # did not is exactly the thing you need to hear about.
  local service_env="${VOICE_SERVICE_ENV:-}"
  if [[ -z "$service_env" ]]; then
    return 0
  fi
  if [[ ! -r "$service_env" ]]; then
    printf '[warn] VOICE_SERVICE_ENV=%s is not readable — skipping. If you meant to source\n' "$service_env" >&2
    printf '       1Password Connect credentials from it, that did NOT happen.\n' >&2
    return 0
  fi
  set -a
  # shellcheck disable=SC1090
  source "$service_env"
  set +a
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
    printf '[fatal] 1Password CLI `op` not found on PATH.\n  On mizuki (Ubuntu) it is /usr/bin/op — install: https://developer.1password.com/docs/cli/get-started\n  On a macOS laptop: brew install 1password-cli\n' >&2
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
