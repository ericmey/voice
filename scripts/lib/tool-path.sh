#!/usr/bin/env bash
#
# Find the tools the ops surface depends on, from ANY shell — or fail with an error an
# operator can act on. Never "command not found".
#
# WHY THIS FILE EXISTS
#
# `make verify-bearers` — the gate that `make deploy` and `make cycle` hard-depend on —
# exited 2 from a clean shell with `uv: command not found`. The binary was right there at
# ~/.local/bin/uv. A non-interactive shell (ssh mizuki '<cmd>', cron, a CI runner) does not
# read the login profile that puts it on PATH.
#
# So the deploy gate was unrunnable from the documented operator surface, and the ONLY reason
# it ever passed was that I hand-exported PATH in front of every ssh command I ran — dozens of
# times, all day. I fixed it in my shell, over and over, and never once in the product. The
# repair became invisible to me precisely BECAUSE I was so used to typing it.
#
# docs/AGENT-LESSONS.md, first entry, 2026-05-22:
#
#   "Any operator script that calls credentialed CLIs must load the same 1Password-backed env
#    template the deployed service uses, or fail with an explicit credential-bootstrap error.
#    Do not rely on an interactive shell's PATH or pre-exported secrets."
#
#   "Why: Health checks that fail from a clean shell train operators to ignore them."
#
# The lesson was already written, at the top of the file I am supposed to read before doing
# non-trivial work in this repo, and it names this exact failure — down to the PATH. Writing
# a lesson down is not the same as carrying it. (Yua, round 6.)
#
# Usage:
#   source "${REPO_ROOT}/scripts/lib/tool-path.sh"
#   ensure_tool uv       # exits 78 with an actionable message if truly absent
#   ensure_tools uv lk jq

# Where these things actually get installed, across the machines we run on. Searched in order
# only when the tool is not already on PATH.
_TOOL_SEARCH_DIRS=(
  "${HOME}/.local/bin"      # uv's default installer target — the one that bit us
  "${HOME}/.cargo/bin"
  /usr/local/bin
  /opt/homebrew/bin         # macOS arm64
  /home/linuxbrew/.linuxbrew/bin
  /snap/bin
)

# Resolve one tool onto PATH. Prints nothing on success.
ensure_tool() {
  local tool="$1"

  if command -v "$tool" >/dev/null 2>&1; then
    return 0
  fi

  local dir
  for dir in "${_TOOL_SEARCH_DIRS[@]}"; do
    if [[ -x "${dir}/${tool}" ]]; then
      export PATH="${dir}:${PATH}"
      return 0
    fi
  done

  # FAIL EXPLICITLY. An operator reading this must know what is missing, where we looked, and
  # what to do — not be handed "command not found" from three subshells down.
  printf '\033[1;31m[fatal]\033[0m %s not found.\n' "$tool" >&2
  printf '        PATH=%s\n' "${PATH}" >&2
  printf '        also searched: %s\n' "${_TOOL_SEARCH_DIRS[*]}" >&2
  case "$tool" in
    uv) printf '        install: curl -LsSf https://astral.sh/uv/install.sh | sh\n' >&2 ;;
    lk) printf '        install: https://docs.livekit.io/home/cli/cli-setup/\n' >&2 ;;
    *)  printf '        install %s and re-run.\n' "$tool" >&2 ;;
  esac
  printf '        (If it IS installed, this shell is non-interactive and never read your\n' >&2
  printf '         profile. That is the bug this file exists to make impossible.)\n' >&2
  exit 78  # EX_CONFIG — the environment is wrong, not the code
}

ensure_tools() {
  local tool
  for tool in "$@"; do
    ensure_tool "$tool"
  done
}
