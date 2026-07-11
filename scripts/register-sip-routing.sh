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

# shellcheck source=scripts/lib/tool-path.sh
source "${REPO_ROOT}/scripts/lib/tool-path.sh"

# shellcheck source=scripts/lib/livekit-env.sh
source "${REPO_ROOT}/scripts/lib/livekit-env.sh"
livekit_reexec_with_1password_if_needed "${REPO_ROOT}" "${SCRIPT_PATH}" "$@"

# The fleet, named. The old loop globbed `sip-dispatch-*.json` and registered whatever
# it found — so a stray file became a live routing rule, and ZERO files became a warning
# and exit 0 (`make up` would report success having registered no routing at all).
AGENTS=(nyla aoi yua sumi)

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

# Install hints must match the host this actually runs on. It runs on mizuki (Ubuntu) — the
# old `brew install` hints were fossils from the retired macOS deploy and could not be
# followed. `make up` calls this script, so a fatal here means `make up` fails; it has been
# failing on the host, because `lk` is not installed there (SIP routing survives only because
# LiveKit persists dispatch rules from a previous registration).
_install_hint() {
  if [[ "$(uname -s)" == "Darwin" ]]; then
    printf 'brew install %s' "$1"
  else
    printf 'see https://docs.livekit.io/home/cli/cli-setup/  (or: apt install %s)' "$1"
  fi
}
# Resolve from any shell — a non-interactive ssh/cron shell never read the login profile,
# which is how `make up` came to fail on the very host it is documented to run on.
ensure_tools lk jq
# Preflight is not optional. Without it this script deletes live dispatch rules and replaces
# them with files nothing has validated — so a missing `uv` must STOP the run, not skip the
# check. Refusing to register is safe; registering unvalidated is how a caller reaches the
# wrong sister.
# Preflight is not optional: without it this script deletes live dispatch rules and replaces
# them with files nothing has validated. A missing `uv` STOPS the run.
ensure_tool uv

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

# ---- PREFLIGHT: validate the COMPLETE set before touching anything -------------
#
# register_rule() DELETES the live rule and only then creates the replacement, from a
# file it has never looked at. So a truncated file, a bad agentName, or a DID pasted
# from a sister lands as: working route destroyed, broken route in its place. Nobody
# finds out until a caller does.
#
# Per-file validation inside the loop would not save us either — by the time rule
# three is found bad, rules one and two are already deleted. Duplicate DIDs and a
# missing sister are only visible holding all four at once.
#
# So: one validator (sdk/sip_preflight.py — the SAME code the tests run, which is what
# stops CI from certifying the .example files while production reads the .json ones),
# run over the whole candidate set, BEFORE the first delete. If it fails, we have not
# mutated anything and the old routing is still carrying calls.
log "preflight: validating the dispatch set in ${CONFIG_DIR}"
if ! (cd "${REPO_ROOT}" && uv run python -m sdk.sip_preflight "${CONFIG_DIR}"); then
  die "dispatch preflight failed — nothing was registered, live routing is untouched"
fi

for a in "${AGENTS[@]}"; do
  register_rule "${CONFIG_DIR}/sip-dispatch-${a}.json"
done

# ---- POSTCONDITION: the live routing must EQUAL what we validated -----------------
#
# "The command exited 0" is not "the four of them are routable", and — the subtler one —
# "all four names are present" is not "the routing is correct". A stale rule the registrar
# never looks at (it only deletes the four names it knows) can still be sitting there
# claiming one of our DIDs, with all four expected names present. Subset, not equality.
#
# So hand the live list to the SAME validator that approved the candidates and require an
# exact match: four rules, exact identities, exact DID ownership, no extras, no duplicates.
if ! $DRY_RUN; then
  log "verifying live dispatch equals the validated set"
  if ! lk sip dispatch list --json 2>/dev/null \
      | (cd "${REPO_ROOT}" && uv run python -m sdk.sip_preflight "${CONFIG_DIR}" --live -); then
    die "live dispatch does NOT match what we just registered — inbound routing is not what we validated"
  fi
fi

log "done."
exit 0
