#!/usr/bin/env bash
# provision-sumi-llm-key.sh (v11) — mint Sumi's scoped LiteLLM key and store it in
# 1Password via TRANSACTION INVERSION with a unique correlation, an exclusive lock, and
# a HASH-CORRELATED identity proof. ARTIFACT for review (GO-SECRET-A); NOT run until
# execution is authorized (GO-SECRET-B). Run from the voice repo root.
#
# GUARANTEES (each exercised by scripts/test-provision-sumi-llm-key.sh against a LOCAL
# FAKE HTTP server running the REAL embedded Python — no fabricated receipts):
#
#  G1 SECRET PATH — the child key crosses only stdout PIPEs (mint -> jq -> `op item edit
#     <id>` stdin). verify-C reads the stored key from injected env and NEVER prints it;
#     only sha256 HASHES ever cross a process boundary for identity.
#  G2 MASTER NEVER LEAVES — mint/revoke/info run in-container via an in-process HTTP
#     client reading LITELLM_MASTER_KEY from the container env; never in argv.
#  G3 FAIL-CLOSED PREFLIGHT — BOTH the 1P item and the alias are classified via a guarded
#     wrapper: capture output AND rc, proceed ONLY on rc==0 AND exactly "absent"; an
#     exhaustive `*)` arm aborts. A docker/op/exec failure (empty output, nonzero) is
#     UNKNOWN -> abort, never a fall-through to "proceed" (B1).
#  G4 TRANSACTION INVERSION + UNIQUE CORRELATION — non-secret placeholder created FIRST,
#     stamped with a unique per-run correlation, id captured; mint; edit THAT id. Cleanup
#     finds OUR item by correlation (covers a create commit-plus-response-loss that left
#     ITEM_ID empty). The correlation is NEVER externally overridable outside explicit
#     test mode (B4).
#  G5 VERIFIED CLEANUP — on any non-success: revoke alias, delete every correlated item,
#     then PROVE alias-absent AND no-correlated-item-remains. Every state read in cleanup
#     captures rc WITHOUT tripping set -e, so a docker failure surfaces as
#     CLEANUP_INCOMPLETE (rc 4), never a silent early exit (B2).
#  G6 STORED-SECRET IDENTITY (correlated) — verify-B (master/admin) asserts alias exists,
#     key_alias==ALIAS, models==[MODEL], and captures the alias's stored token HASH.
#     verify-C authenticates the STORED key (op-run injected) and asserts BOTH: (a) its
#     sha256 equals the alias's stored token hash (so the stored key BELONGS TO this exact
#     alias, not merely some sumi-scoped key — B3), and (b) /v1/models is exactly [MODEL].
#     The raw key is never printed; correlation is hash-only (hash_token = sha256 hexdigest,
#     verified in litellm source).
#  G7 EXCLUSIVE LOCK — an atomic mkdir lock on the canonical host is acquired before
#     preflight; a concurrent run cannot both-preflight-absent then cross-revoke (B4).
#
# Usage:  ./scripts/provision-sumi-llm-key.sh [--dry-run]
set -euo pipefail

ALIAS="sumi-voice-worker-v2"
TITLE="sumi-voice-litellm-api-key"
VAULT="Harem World"
MODEL="sumi"
LLM_CTR="${LLM_CTR:-litellm}"
LLM_URL="${LLM_URL:-http://127.0.0.1:4000}"
OP="${OP_BIN:-op}"
# Least-privilege injection surface (D1): verify-C resolves ONLY the Sumi key, never the
# shared livekit.env.tpl (8 unrelated refs after E1 removed Sumi from it). The Sumi-only
# template is what production uses; a test-mode override lets the harness point it at the
# broad template to prove the pre-mutation validation and runtime sentinel both fire.
SUMI_ONLY_TPL="config/sumi-llm-key.env.tpl"
# The EXACT and ONLY active assignment the Sumi-only template may contain (E2). Template
# validation runs BEFORE any mutation, so an unknown FUTURE op-backed ref (not just today's
# eight) can never reach verify-C — it is caught by exact-set validation, not a name blocklist.
EXPECTED_SUMI_REF="SUMI_LLM_API_KEY=op://Harem World/sumi-voice-litellm-api-key/credential"
PLACEHOLDER="PENDING-PROVISION"
CORR_FIELD="provision_run"
CANONICAL_LOCKDIR="/tmp/sumi-voice-litellm-provision.lock.d"   # FIXED prod path (C2)
# Runtime defense-in-depth (D1): op-backed names that MUST NOT reach verify-C's process env —
# every non-Sumi ref in livekit.env.tpl. verify-C also fails closed on any of these, on top of
# the pre-mutation exact-set template validation above.
FORBIDDEN_ENV="LIVEKIT_API_KEY LIVEKIT_API_SECRET OPENAI_API_KEY GOOGLE_API_KEY ELEVENLABS_API_KEY MUSUBI_V2_TOKEN_AOI MUSUBI_V2_TOKEN_NYLA MUSUBI_V2_TOKEN_YUA"

RC_ABORT_EXISTS=3
RC_CLEANUP_INCOMPLETE=4
RC_LOCKED=5
RC_LOCK_RELEASE_FAILED=6
RC_ABORT_LOCK_STALE=7        # no-mutation abort BUT lock could not be released (D3) — NOT provisioned
RC_TEMPLATE_INVALID=8        # the Sumi-only template is not EXACTLY one expected ref (E2) — no mutation

# Correlation, lock path, AND the verify template are generated/fixed internally and are NOT
# externally overridable except in explicit test mode (B4/C2/D1) — an attacker-supplied
# correlation could force a cross-delete, an overridable lock path defeats exclusivity, and a
# swapped verify template could smuggle unrelated secrets past the sentinel.
if [ "${PROVISION_TEST_MODE:-0}" = "1" ]; then
  CORRELATION="${PROVISION_CORRELATION:-sumi-provision-TEST-$$-${RANDOM}${RANDOM}}"
  LOCKDIR="${PROVISION_LOCKDIR:-$CANONICAL_LOCKDIR}"
  VERIFY_TPL="${PROVISION_VERIFY_TPL:-$SUMI_ONLY_TPL}"
else
  CORRELATION="sumi-provision-$(date +%Y%m%dT%H%M%S)-$$-${RANDOM}${RANDOM}"
  LOCKDIR="$CANONICAL_LOCKDIR"   # PROVISION_LOCKDIR is IGNORED in production
  VERIFY_TPL="$SUMI_ONLY_TPL"    # PROVISION_VERIFY_TPL is IGNORED in production
fi

log() { printf '[provision] %s\n' "$*" >&2; }

# validate_sumi_only_template (E2): the injection surface must contain EXACTLY ONE active
# (non-comment, non-blank) assignment, and it must be EXACTLY the expected Sumi ref. Any extra
# ref (including an unknown future op-backed name), a duplicate, a wrong URI, or a missing Sumi
# ref -> non-zero. This is the pre-mutation guarantee that the surface is Sumi-only, independent
# of the runtime name blocklist. rc 0 == valid.
validate_sumi_only_template() {
  local tpl="$1" line active=0 sawsumi=0 stripped
  [ -f "$tpl" ] || { log "template-validate: '$tpl' not found"; return 1; }
  while IFS= read -r line || [ -n "$line" ]; do
    stripped="${line#"${line%%[![:space:]]*}"}"      # left-trim
    case "$stripped" in ''|\#*) continue ;; esac      # blank / comment -> ignore
    active=$((active + 1))
    if [ "$stripped" = "$EXPECTED_SUMI_REF" ]; then sawsumi=$((sawsumi + 1)); fi
  done < "$tpl"
  if [ "$active" -eq 1 ] && [ "$sawsumi" -eq 1 ]; then return 0; fi
  log "template-validate: '$tpl' is not Sumi-only (active_lines=$active, exact_sumi_refs=$sawsumi; expected exactly 1/1)"
  return 1
}

# ---------------------------------------------------------------------------
# in-container primitives (G2)
# ---------------------------------------------------------------------------
_mint_in_container() {
  docker exec "$LLM_CTR" python3 - "$ALIAS" "$MODEL" "$LLM_URL" <<'PY'
import os, sys, json, urllib.request
alias, model, base = sys.argv[1], sys.argv[2], sys.argv[3]
mk = os.environ.get("LITELLM_MASTER_KEY")
if not mk:
    sys.stderr.write("no master key in container env\n"); sys.exit(2)
hdr = {"Authorization": "Bearer " + mk, "Content-Type": "application/json"}
def _post(path, payload):
    req = urllib.request.Request(base + path, data=json.dumps(payload).encode(),
                                 headers=hdr, method="POST")
    return urllib.request.urlopen(req, timeout=20)
def _revoke():
    try: _post("/key/delete", {"key_aliases": [alias]}).read()
    except Exception: pass
try:
    resp = _post("/key/generate", {"models": [model], "key_alias": alias})
    body = resp.read()
    if resp.status // 100 != 2:
        _revoke(); sys.stderr.write("mint non-2xx -> revoked\n"); sys.exit(1)
    key = (json.loads(body) or {}).get("key")
    if not isinstance(key, str) or not key.startswith("sk-") or len(key) < 20:
        _revoke(); sys.stderr.write("mint 2xx but missing/malformed key -> revoked\n"); sys.exit(1)
    sys.stdout.write(key)
except SystemExit:
    raise
except Exception as e:
    _revoke(); sys.stderr.write("mint uncertain (%s) -> revoked\n" % type(e).__name__); sys.exit(1)
PY
}

revoke_alias() {
  docker exec "$LLM_CTR" python3 - "$ALIAS" "$LLM_URL" <<'PY' || true
import os, sys, json, urllib.request
alias, base = sys.argv[1], sys.argv[2]
mk = os.environ.get("LITELLM_MASTER_KEY") or ""
req = urllib.request.Request(base + "/key/delete",
      data=json.dumps({"key_aliases": [alias]}).encode(),
      headers={"Authorization": "Bearer " + mk, "Content-Type": "application/json"}, method="POST")
try: urllib.request.urlopen(req, timeout=20).read()
except Exception: pass
PY
}

# alias_state (C1): prints present|absent|unknown via a POSITIVE enumeration, never by
# interpreting a 404. `GET /key/list?key_alias=<alias>` is an EXACT-match filter (verified
# in litellm source) returning {"keys":[...],"total_count":int,...}. Absence is proven ONLY
# by a SUCCESSFUL 2xx whose total_count==0; total_count>=1 is present; ANY non-2xx (a
# proxy/wrong-path/unsupported-endpoint 404 included), missing total_count, or parse error
# is UNKNOWN. A generic 404 can no longer masquerade as "this alias is absent".
# NOTE: if `docker exec` itself fails there is NO output + nonzero rc — callers use _classify.
alias_state() {
  docker exec "$LLM_CTR" python3 - "$ALIAS" "$LLM_URL" <<'PY'
import os, sys, json, urllib.request, urllib.error, urllib.parse
alias, base = sys.argv[1], sys.argv[2]
mk = os.environ.get("LITELLM_MASTER_KEY") or ""
url = base + "/key/list?return_full_object=false&key_alias=" + urllib.parse.quote(alias, safe="")
req = urllib.request.Request(url, headers={"Authorization": "Bearer " + mk})
try:
    d = json.loads(urllib.request.urlopen(req, timeout=20).read() or b"{}")
except Exception:
    print("unknown"); sys.exit(0)          # non-2xx (incl. generic 404) / conn / parse -> UNKNOWN
tc = d.get("total_count")
keys = d.get("keys")
# D2: bool is an int subclass, so `isinstance(tc,int)` would let total_count=false or -1
# fall through to "absent". Require an EXACT int (type is int, not bool), a non-negative
# count, a list of keys, and count/keys AGREEMENT. Anything else is UNKNOWN, never absent.
if type(tc) is not int or not isinstance(keys, list):
    print("unknown")
elif tc == 0 and len(keys) == 0:
    print("absent")                         # a SUCCESSFUL exact-match enumeration, consistently empty
elif tc >= 1 and len(keys) >= 1:
    print("present")
else:
    print("unknown")                        # negative tc, or total_count/keys disagreement
PY
}

# verify-B: master/admin /key/info by alias asserts key_alias AND models (F4)
key_alias_and_models_ok() {
  docker exec "$LLM_CTR" python3 - "$ALIAS" "$LLM_URL" "$MODEL" <<'PY'
import os, sys, json, urllib.request, urllib.parse
alias, base, model = sys.argv[1], sys.argv[2], sys.argv[3]
mk = os.environ.get("LITELLM_MASTER_KEY") or ""
url = base + "/key/info?key_alias=" + urllib.parse.quote(alias, safe="")
req = urllib.request.Request(url, headers={"Authorization": "Bearer " + mk})
try:
    d = json.loads(urllib.request.urlopen(req, timeout=20).read() or b"{}")
except Exception:
    sys.exit(1)
info = d.get("info") if isinstance(d.get("info"), dict) else d
models = (info.get("models") if isinstance(info, dict) else None) or []
sys.exit(0 if (isinstance(info, dict) and info.get("key_alias") == alias and models == [model]) else 1)
PY
}

# alias_token_hash: master/admin lookup by alias; prints the alias's stored token HASH
# (sha256 hexdigest — NON-secret). Empty/rc1 if unavailable (fail-closed at the caller).
alias_token_hash() {
  docker exec "$LLM_CTR" python3 - "$ALIAS" "$LLM_URL" <<'PY'
import os, sys, json, urllib.request, urllib.parse
alias, base = sys.argv[1], sys.argv[2]
mk = os.environ.get("LITELLM_MASTER_KEY") or ""
url = base + "/key/info?key_alias=" + urllib.parse.quote(alias, safe="")
req = urllib.request.Request(url, headers={"Authorization": "Bearer " + mk})
try:
    d = json.loads(urllib.request.urlopen(req, timeout=20).read() or b"{}")
except Exception:
    sys.exit(1)
info = d.get("info") if isinstance(d.get("info"), dict) else d
tok = info.get("token") if isinstance(info, dict) else None
if not isinstance(tok, str) or not tok:
    sys.exit(1)
sys.stdout.write(tok)
PY
}

# ---------------------------------------------------------------------------
# 1Password state via SUCCESSFUL enumeration (G3/G5) — never error-string parsing (F2)
# ---------------------------------------------------------------------------
# STRICT list schema (F1/G1): a SUCCESSFUL response is trustworthy only if it is a top-level
# JSON array whose every entry is an object with a NON-EMPTY string `id` AND a NON-EMPTY string
# `title`. Rejects {} / null / 42 / [null] / [{"id":..}] AND an empty-string id (which
# _our_item_ids would otherwise skip as a false-empty proof). `and` short-circuits so a
# non-array never reaches `all(.[]; …)` and never errors.
_LIST_SCHEMA='type=="array" and all(.[]; type=="object" and (.id|type=="string" and length>0) and (.title|type=="string" and length>0))'

op_item_state() {   # $1 = title ; prints present|absent|unknown (rc 0 always)
  local list rc
  list="$("$OP" item list --vault "$VAULT" --format json 2>/dev/null)" && rc=0 || rc=$?
  [ "$rc" -eq 0 ] || { echo unknown; return 0; }
  echo "$list" | jq -e "$_LIST_SCHEMA" >/dev/null 2>&1 || { echo unknown; return 0; }   # malformed success -> UNKNOWN
  if echo "$list" | jq -e --arg t "$1" 'any(.[]; .title==$t)' >/dev/null 2>&1; then echo present; else echo absent; fi
}

# _classify: run a classifier; emit present|absent|unknown, mapping ANY failure/empty/
# unexpected output to unknown. Always rc 0 (B1: no fall-through fail-open).
_classify() {
  local out rc
  out="$("$@")" && rc=0 || rc=$?
  if [ "$rc" != "0" ]; then echo unknown; return 0; fi
  case "$out" in present|absent|unknown) printf '%s\n' "$out" ;; *) echo unknown ;; esac
}

# _our_item_ids: ids of items with our TITLE bearing OUR correlation (G4). rc 0 proven, 2 unknown.
# Applies the strict list schema (F1) AND a strict item-get schema (G1) that BINDS the returned
# .id to the requested id and requires every field to be a well-formed object (non-empty string
# id), with the correlation field's value a non-empty string when present — so a wrong/malformed
# item body can NEVER be silently read as "not ours" (which would let cleanup claim VERIFIED).
_our_item_ids() {
  local list ids id got out=""
  list="$("$OP" item list --vault "$VAULT" --format json 2>/dev/null)" || return 2
  echo "$list" | jq -e "$_LIST_SCHEMA" >/dev/null 2>&1 || return 2     # malformed list -> UNKNOWN (return 2)
  ids="$(echo "$list" | jq -r --arg t "$TITLE" '.[] | select(.title==$t) | .id')" || return 2
  while IFS= read -r id; do
    [ -n "$id" ] || continue
    got="$("$OP" item get "$id" --vault "$VAULT" --format json 2>/dev/null)" || return 2
    # G1 strict item-get: object whose .id EXACTLY equals the requested id; fields an array of
    # well-formed objects (non-empty string id); the correlation field (if present) a non-empty
    # string value. Any mismatch -> return 2, NEVER silently "not correlated".
    echo "$got" | jq -e --arg id "$id" --arg f "$CORR_FIELD" '
        type=="object" and .id==$id and (.fields|type=="array")
        and all(.fields[]; type=="object" and (.id|type=="string" and length>0)
                and (if .id==$f then (.value|type=="string" and length>0) else true end))
      ' >/dev/null 2>&1 || return 2
    if echo "$got" | jq -e --arg f "$CORR_FIELD" --arg c "$CORRELATION" \
         'any(.fields[]; .id==$f and .value==$c)' >/dev/null 2>&1; then
      out="$out$id"$'\n'
    fi
  done <<< "$ids"
  printf '%s' "$out"
  return 0
}

delete_item_by_id() {
  local id="${1:-}"
  [ -n "$id" ] && [ "$id" != "null" ] || return 0
  "$OP" item delete "$id" --vault "$VAULT" >/dev/null 2>&1 || true
}

# ---------------------------------------------------------------------------
# lock (G7) + verified cleanup (G5)
# ---------------------------------------------------------------------------
LOCK_HELD=0
# release_lock (C3): VERIFY the lock actually went away. rmdir failure is no longer
# swallowed — if the dir persists we return non-zero so the caller can surface it rather
# than silently reporting a clean success while leaving a stale lock.
release_lock() {
  [ "$LOCK_HELD" = "1" ] || return 0
  rmdir "$LOCKDIR" 2>/dev/null
  if [ -d "$LOCKDIR" ]; then return 1; fi   # still present -> release UNVERIFIED
  LOCK_HELD=0
  return 0
}

# OUTCOME (D3): three DISTINCT terminal states so a no-mutation abort can never borrow the
# "credentials are provisioned" meaning just to suppress rollback.
#   running     -> a failure occurred mid-transaction -> roll back (+ verified cleanup)
#   provisioned -> full success -> keep credentials; a stale lock is rc6 (still provisioned)
#   abort       -> preflight/lock refused, NOTHING was created -> keep abort rc; a stale lock
#                  is rc7 (explicitly NOT provisioned), never rc6
OUTCOME="running"
ITEM_ID=""
cleanup() {
  if [ "$OUTCOME" = "provisioned" ]; then
    # Credentials are provisioned and valid — do NOT roll back. If the lock cannot be
    # removed, this is not a fully clean success: surface it distinctly (C3).
    if ! release_lock; then
      log "LOCK-RELEASE-FAILED: credentials for '$TITLE' ARE provisioned and valid, but the exclusive lock ($LOCKDIR) could not be removed — remove it manually before the next run"
      exit "$RC_LOCK_RELEASE_FAILED"
    fi
    return 0
  fi
  if [ "$OUTCOME" = "abort" ]; then
    # A no-mutation abort: NO mint, create, revoke, or delete happened. Release the lock we
    # acquired; if that fails, say so TRUTHFULLY — no credentials exist, so this is rc7, not
    # the provisioned-success rc6 (D3).
    if ! release_lock; then
      log "LOCK-STALE-AFTER-ABORT: NO credentials were created (no mint/create/revoke/delete); the exclusive lock ($LOCKDIR) could not be removed — remove it manually. This is NOT a provisioned success."
      exit "$RC_ABORT_LOCK_STALE"
    fi
    return 0
  fi
  log "cleanup (not successful): revoke alias + delete correlated item(s), then VERIFY (corr=$CORRELATION)"
  revoke_alias
  local ids rc
  ids="$(_our_item_ids)" && rc=0 || rc=$?
  if [ "$rc" != "0" ]; then
    log "CLEANUP-INCOMPLETE: could not enumerate 1Password to find our item(s) (alias=$ALIAS corr=$CORRELATION)"
    release_lock || log "cleanup: lock release also failed ($LOCKDIR)"; exit "$RC_CLEANUP_INCOMPLETE"
  fi
  while IFS= read -r id; do [ -n "$id" ] && delete_item_by_id "$id"; done <<< "$ids"
  local a arc incomplete=0 remain rrc
  a="$(alias_state)" && arc=0 || arc=$?          # B2: capture rc, never let set -e skip the check
  if [ "$arc" != "0" ] || [ "$a" != "absent" ]; then incomplete=1; log "cleanup: alias state='$a' rc=$arc (not proven absent)"; fi
  remain="$(_our_item_ids)" && rrc=0 || rrc=$?
  if [ "$rrc" != "0" ]; then
    incomplete=1; log "cleanup: re-enumeration failed (item absence UNPROVEN)"
  elif [ -n "$(printf '%s' "$remain" | tr -d '[:space:]')" ]; then
    incomplete=1; log "cleanup: correlated item(s) still present (not proven absent)"
  fi
  if [ "$incomplete" = "1" ]; then
    log "CLEANUP-INCOMPLETE: rollback could not be PROVEN — manual check required (alias=$ALIAS corr=$CORRELATION)"
    release_lock || log "cleanup: lock release also failed ($LOCKDIR)"; exit "$RC_CLEANUP_INCOMPLETE"
  fi
  log "cleanup verified: alias absent AND no correlated item remains"
  release_lock || log "cleanup: lock release failed after verified rollback ($LOCKDIR)"
}
trap cleanup EXIT

# ---------------------------------------------------------------------------
# dry-run (no lock, no side effects)
# ---------------------------------------------------------------------------
if [ "${1:-}" = "--dry-run" ]; then
  cat >&2 <<PLAN
DRY RUN — no docker / op invoked. Branch map (v11):
  lock    atomic mkdir $LOCKDIR (fixed canonical; env-overridable only in test mode) ; already held -> ABORT($RC_LOCKED)
  preflight(item,alias via _classify) absent->continue ; present|unknown|exec-fail->ABORT($RC_ABORT_EXISTS)
  template validate $VERIFY_TPL is EXACTLY the one Sumi ref (E2) ; else ABORT($RC_TEMPLATE_INVALID), no mutation
  invert  create placeholder stamped $CORR_FIELD=<internal corr> -> capture ITEM_ID (empty=>commit-drop->trap reconciles)
  mint    in-container: 2xx+valid->emit ; else self-revoke+exit1
  store   key(pipe) -> jq (FAIL unless exactly 1 credential field) -> \`op item edit ITEM_ID\` (stdin)
  verify  A op read resolves ; B alias==$ALIAS AND models==["$MODEL"] (master) + capture token HASH ;
          C op run --env-file=$VERIFY_TPL (Sumi-ONLY) -> sentinel: NO forbidden secret present ;
            stored key sha256 == alias token hash (BELONGS-TO) AND /v1/models==["$MODEL"] (SCOPE)
  fail@any-> OUTCOME=running -> trap: revoke + delete correlated + PROVE both absent (rc-guarded) ; unproven->CLEANUP_INCOMPLETE($RC_CLEANUP_INCOMPLETE)
  preflight/lock refused -> OUTCOME=abort (NO mutation) ; stale lock on abort -> $RC_ABORT_LOCK_STALE (never provisioned rc6)
  all pass -> OUTCOME=provisioned -> trap releases lock ; stale lock -> $RC_LOCK_RELEASE_FAILED (still provisioned)
PLAN
  OUTCOME="abort"
  exit 0
fi

# ---------------------------------------------------------------------------
# 0. EXCLUSIVE LOCK (G7/B4) — before any preflight/mutation
# ---------------------------------------------------------------------------
if ! mkdir "$LOCKDIR" 2>/dev/null; then
  log "ABORT: another provisioning run holds the lock ($LOCKDIR) — refusing to run concurrently"
  # We do NOT own this lock (LOCK_HELD stays 0), so release_lock is a no-op. OUTCOME=abort so
  # the trap takes the no-mutation path, not rollback.
  OUTCOME="abort"
  exit "$RC_LOCKED"
fi
LOCK_HELD=1
log "acquired exclusive lock ($LOCKDIR)"

# ---------------------------------------------------------------------------
# 1. PREFLIGHT — confirmed-absent for BOTH; exhaustive arms; exec-failure aborts (B1)
# ---------------------------------------------------------------------------
case "$(_classify op_item_state "$TITLE")" in
  absent)  : ;;
  present) log "ABORT: 1Password item '$TITLE' already exists"; OUTCOME="abort"; exit "$RC_ABORT_EXISTS" ;;
  *)       log "ABORT: 1Password item state UNKNOWN — refusing to assume absent"; OUTCOME="abort"; exit "$RC_ABORT_EXISTS" ;;
esac
case "$(_classify alias_state)" in
  absent)  : ;;
  present) log "ABORT: LiteLLM alias '$ALIAS' already exists"; OUTCOME="abort"; exit "$RC_ABORT_EXISTS" ;;
  *)       log "ABORT: LiteLLM alias state UNKNOWN — refusing to assume absent"; OUTCOME="abort"; exit "$RC_ABORT_EXISTS" ;;
esac
# E2: prove the injection surface is EXACTLY the one Sumi ref BEFORE any mutation. Catches an
# unknown future op-backed ref that the runtime name blocklist would miss.
if ! validate_sumi_only_template "$VERIFY_TPL"; then
  log "ABORT: verify template '$VERIFY_TPL' is not Sumi-only — refusing to provision (no mutation)"
  OUTCOME="abort"; exit "$RC_TEMPLATE_INVALID"
fi
log "preflight: 1P item + alias both CONFIRMED ABSENT; verify template is Sumi-only — proceeding (corr=$CORRELATION)"

# ---------------------------------------------------------------------------
# 2. TRANSACTION INVERSION — placeholder stamped with the internal correlation (G4)
# ---------------------------------------------------------------------------
ITEM_ID="$(
  "$OP" item create --vault "$VAULT" --format json - <<JSON | jq -r '.id'
{"title": "$TITLE", "category": "API_CREDENTIAL", "vault": "$VAULT",
 "fields": [{"id": "credential", "type": "CONCEALED", "value": "$PLACEHOLDER"},
            {"id": "$CORR_FIELD", "type": "STRING", "label": "$CORR_FIELD", "value": "$CORRELATION"}]}
JSON
)"
if [ -z "$ITEM_ID" ] || [ "$ITEM_ID" = "null" ]; then
  log "FAIL: placeholder create returned no id (possible commit-drop) — trap reconciles by correlation"
  exit 1
fi
log "placeholder item created id=$ITEM_ID ($CORR_FIELD=$CORRELATION)"

PLACEHOLDER_JSON="$("$OP" item get "$ITEM_ID" --vault "$VAULT" --format json)"
if [ -z "$PLACEHOLDER_JSON" ]; then
  log "FAIL: could not read back placeholder item for patch"; exit 1
fi

# ---------------------------------------------------------------------------
# 3. MINT | PATCH | EDIT-BY-ID (key exists only inside this pipe — G1; F5a guard)
# ---------------------------------------------------------------------------
log "minting scoped key and updating item id=$ITEM_ID (key stays in the pipe)"
set +e
_mint_in_container \
  | jq -Rs --argjson tpl "$PLACEHOLDER_JSON" '
      (. | rtrimstr("\n")) as $k
      | ($tpl.fields | map(select(.id == "credential")) | length) as $n
      | if $n != 1 then error("expected exactly one credential field, got \($n)")
        elif ($k | length) < 20 then error("empty/short key from mint")
        else ($tpl | .fields = [ .fields[]
              | if .id == "credential" then .value = $k else . end ]) end' \
  | "$OP" item edit "$ITEM_ID" >/dev/null
rc=$?
set -e
if [ "$rc" -ne 0 ]; then
  log "FAIL: mint-or-edit pipeline failed (rc=$rc)"; exit 1
fi
log "item id=$ITEM_ID updated with the scoped credential"

# ---------------------------------------------------------------------------
# 4. VERIFY (non-secret rc/metadata + HASH correlation; raw value never printed)
# ---------------------------------------------------------------------------
"$OP" read "op://$VAULT/$TITLE/credential" >/dev/null 2>&1 \
  || { log "FAIL(verify A): op:// reference does not resolve"; exit 1; }
key_alias_and_models_ok \
  || { log "FAIL(verify B): key alias/models != {$ALIAS,[$MODEL]}"; exit 1; }

H_ALIAS="$(alias_token_hash)" && hrc=0 || hrc=$?
if [ "$hrc" != "0" ] || [ -z "$H_ALIAS" ]; then
  log "FAIL(verify C-pre): could not read alias token hash (admin /key/info token field)"; exit 1
fi
# verify-C (G6/B3/D1): run under the Sumi-ONLY injection surface ($VERIFY_TPL), and prove:
#   (0) LEAST PRIVILEGE — NO forbidden op-backed secret reached this process (D1);
#   (a) BELONGS-TO — the stored key's sha256 equals the alias's stored token hash;
#   (b) SCOPE — the stored key sees exactly [MODEL] via /v1/models.
# Key never printed; only hashes cross. set +e so the probe's non-zero reaches `vcrc`.
set +e
V_LLM_URL="$LLM_URL" V_MODEL="$MODEL" V_HASH="$H_ALIAS" V_FORBIDDEN="$FORBIDDEN_ENV" OP_BIN="$OP" \
  "$OP" run --env-file="$VERIFY_TPL" -- python3 - <<'PY'
import os, sys, json, hashlib, urllib.request
# (0) LEAST-PRIVILEGE SENTINEL (D1): this provisioning check must run on a Sumi-only surface.
# If ANY unrelated op-backed secret is present and non-empty, the injection surface is too
# broad — fail closed rather than quietly running with eight extra secrets in the env.
leaked = [n for n in (os.environ.get("V_FORBIDDEN") or "").split() if os.environ.get(n)]
if leaked:
    sys.stderr.write("least-privilege violation: unrelated secret(s) injected: %s\n" % ",".join(leaked))
    sys.exit(5)
k = os.environ.get("SUMI_LLM_API_KEY") or ""
if not k:
    sys.exit(1)
base = (os.environ.get("V_LLM_URL") or "").rstrip("/")
model = os.environ.get("V_MODEL") or ""
want = os.environ.get("V_HASH") or ""
# (a) BELONGS-TO: stored key hashes to the alias's stored token hash (litellm hash_token = sha256 hex)
if not want or hashlib.sha256(k.encode()).hexdigest() != want:
    sys.exit(2)
# (b) SCOPE: the stored key sees exactly [model]
req = urllib.request.Request(base + "/v1/models", headers={"Authorization": "Bearer " + k})
try:
    d = json.loads(urllib.request.urlopen(req, timeout=20).read() or b"{}")
except Exception:
    sys.exit(3)
ids = [m.get("id") for m in (d.get("data") or [])]
sys.exit(0 if ids == [model] else 4)
PY
vcrc=$?
set -e
[ "$vcrc" -eq 0 ] || { log "FAIL(verify C): stored key not least-privilege/correlated/scoped for alias '$ALIAS' [$MODEL] (rc=$vcrc)"; exit 1; }

# ---------------------------------------------------------------------------
# 5. SUCCESS
# ---------------------------------------------------------------------------
OUTCOME="provisioned"
log "SUCCESS: alias '$ALIAS' + item '$TITLE' (id=$ITEM_ID) provisioned; stored key is alias '$ALIAS' scoped [$MODEL]"
