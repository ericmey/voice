#!/usr/bin/env bash
#
# Idempotent SIP trunk + dispatch-rule registration.
#
# Reads from <repo>/config/ (or $LIVEKIT_CONFIG_DIR):
#   sip-inbound-trunk.json
#   sip-dispatch-nyla.json
#   sip-dispatch-aoi.json
#   sip-dispatch-yua.json
#   sip-dispatch-sumi.json
#
# For each record:
#   1. Look up live state via `lk sip {inbound,dispatch} list --json`.
#   2. If a record with the same name already exists, delete it.
#   3. Re-create from the JSON on disk.
#
# "Delete + recreate" is simpler than "diff + update" and livekit-sip's
# CLI doesn't expose a reliable update path for all fields (dispatch rule
# updates in particular can silently ignore numbers/inbound_numbers
# changes). Deleting is safe because livekit-sip keeps no call history
# on these records — only live routing.
#
# Safety: during the brief window between delete and recreate, inbound
# calls will 486. For near-zero disruption, run during low-traffic.
#
# Usage:
#   scripts/register-sip-routing.sh               # all records
#   scripts/register-sip-routing.sh --dry-run     # show what would change
#   scripts/register-sip-routing.sh --trunks-only # only the inbound trunk
#   scripts/register-sip-routing.sh --rules-only  # only dispatch rules

set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
CONFIG_DIR="${LIVEKIT_CONFIG_DIR:-${REPO_ROOT}/config}"
SCRIPT_PATH="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/$(basename "${BASH_SOURCE[0]}")"

# shellcheck source=scripts/lib/livekit-env.sh
source "${REPO_ROOT}/scripts/lib/livekit-env.sh"
livekit_reexec_with_1password_if_needed "${REPO_ROOT}" "${SCRIPT_PATH}" "$@"

DRY_RUN=false
TRUNKS_ONLY=false
RULES_ONLY=false

while [[ $# -gt 0 ]]; do
  case "$1" in
    --dry-run)      DRY_RUN=true ;;
    --trunks-only)  TRUNKS_ONLY=true ;;
    --rules-only)   RULES_ONLY=true ;;
    *)              echo "unknown flag: $1" >&2; exit 1 ;;
  esac
  shift
done

log()  { printf "\033[1;34m[sip-routing]\033[0m %s\n" "$*"; }
warn() { printf "\033[1;33m[warn]\033[0m        %s\n" "$*"; }
die()  { printf "\033[1;31m[fatal]\033[0m       %s\n" "$*" >&2; exit 1; }

command -v lk >/dev/null 2>&1 || die "lk (livekit-cli) not found — brew install livekit-cli"
command -v jq >/dev/null 2>&1 || die "jq not found — brew install jq"

declare -a TMP_FILES=()
cleanup() {
  local f
  for f in "${TMP_FILES[@]:-}"; do
    if [[ -n "$f" ]]; then
      rm -f "$f"
    fi
  done
  return 0
}
trap cleanup EXIT

wire_json() {
  # The repo's config examples allow a top-level `_comment` field for
  # operator notes. Strip it before handing JSON to LiveKit's proto parser.
  local file="$1"
  local tmp
  tmp="$(mktemp)"
  TMP_FILES+=("$tmp")
  jq 'del(._comment)' "$file" >"$tmp"
  printf '%s\n' "$tmp"
}

# --- trunk helpers ---------------------------------------------------

trunk_id_by_name() {
  # Empty string if not found.
  local name="$1"
  lk sip inbound list --json 2>/dev/null \
    | jq -r --arg n "$name" '.items[]? | select(.name==$n) | .sipTrunkId' \
    | head -1
}

first_trunk_id() {
  # Used to supply --trunks for dispatch rule creation. Warns if there's
  # more than one trunk (unusual; edit the script to disambiguate).
  local all
  all="$(lk sip inbound list --json 2>/dev/null | jq -r '.items[]?.sipTrunkId')"
  local count
  count="$(printf '%s\n' "$all" | grep -c . || true)"
  if [[ $count -eq 0 ]]; then
    die "no inbound trunks found — register one first with --trunks-only"
  fi
  if [[ $count -gt 1 ]]; then
    warn "multiple inbound trunks found — using the first"
  fi
  printf '%s\n' "$all" | head -1
}

register_trunk() {
  local file="${CONFIG_DIR}/sip-inbound-trunk.json"
  [[ -r "$file" ]] || { warn "skipping trunk — $file not found"; return 0; }

  local name
  name="$(jq -r '.trunk.name' "$file")"
  [[ "$name" != "null" && -n "$name" ]] || die "trunk name missing in $file"

  local existing
  existing="$(trunk_id_by_name "$name")"

  if [[ -n "$existing" ]]; then
    if $DRY_RUN; then
      log "DRY: would delete existing trunk '$name' ($existing) and recreate from $file"
      return 0
    fi
    log "deleting existing trunk '$name' ($existing)"
    lk sip inbound delete "$existing" >/dev/null
  fi

  if $DRY_RUN; then
    log "DRY: would create trunk from $file"
    return 0
  fi
  log "creating trunk from $file"
  lk sip inbound create "$(wire_json "$file")" | tail -2
}

# --- dispatch-rule helpers ------------------------------------------

rule_id_by_name() {
  local name="$1"
  lk sip dispatch list --json 2>/dev/null \
    | jq -r --arg n "$name" '.items[]? | select(.name==$n) | .sipDispatchRuleId' \
    | head -1
}

register_rule() {
  local file="$1"
  [[ -r "$file" ]] || { warn "skipping rule — $file not found"; return 0; }

  local name
  name="$(jq -r '.dispatch_rule.name' "$file")"
  [[ "$name" != "null" && -n "$name" ]] || die "dispatch rule name missing in $file"

  local trunk_id
  trunk_id="$(first_trunk_id)"

  local existing
  existing="$(rule_id_by_name "$name")"

  if [[ -n "$existing" ]]; then
    if $DRY_RUN; then
      log "DRY: would delete existing rule '$name' ($existing) and recreate from $file (trunk=$trunk_id)"
      return 0
    fi
    log "deleting existing rule '$name' ($existing)"
    lk sip dispatch delete "$existing" >/dev/null
  fi

  if $DRY_RUN; then
    log "DRY: would create rule from $file (trunk=$trunk_id)"
    return 0
  fi
  log "creating rule from $file (trunk=$trunk_id)"
  lk sip dispatch create --trunks "$trunk_id" "$(wire_json "$file")" | tail -2
}

# --- execute ---------------------------------------------------------

if ! $RULES_ONLY; then
  register_trunk
fi

if $TRUNKS_ONLY; then
  log "done (trunks-only)."
  exit 0
fi

found_rule=false
for rule in "${CONFIG_DIR}"/sip-dispatch-*.json; do
  [[ -e "$rule" ]] || continue
  found_rule=true
  register_rule "$rule"
done
$found_rule || warn "no dispatch rule JSON files found in ${CONFIG_DIR}"

log "done."
if ! $DRY_RUN; then
  log "verify: lk sip inbound list ; lk sip dispatch list"
fi
exit 0
