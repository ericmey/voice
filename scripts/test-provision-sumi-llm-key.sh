#!/usr/bin/env bash
# test-provision-sumi-llm-key.sh (v11) — discriminatory harness for provision-sumi-llm-key.sh.
#
# Fidelity: a LOCAL FAKE LiteLLM server (/key/generate, /key/delete, /key/info WITH the
# stored token hash, /v1/models with bearer auth) runs the helper's REAL embedded Python
# (docker exec -> local python). The fake `op` is stateful (id,title,correlation,STORED
# credential); `op run` injects the ACTUAL stored value, so verify-C authenticates what is
# really stored. The fake `docker` can fail on demand (all, or after N calls) to exercise
# exec-failure paths. No real LiteLLM/1Password; no network beyond 127.0.0.1.
set -uo pipefail
HERE="$(cd "$(dirname "$0")" && pwd)"; REPO="$(cd "$HERE/.." && pwd)"; SCRIPT="$HERE/provision-sumi-llm-key.sh"
for b in python3 jq awk; do command -v "$b" >/dev/null || { echo "$b required"; exit 2; }; done

WORK="$(mktemp -d)"; MOCKBIN="$WORK/bin"; mkdir -p "$MOCKBIN"
RECEIPT="$WORK/receipt"; OPSTATE="$WORK/opstate"; EDITCAP="$WORK/editcap"
LISTCT="$WORK/listct"; DOCKERCT="$WORK/dockerct"; GETCT="$WORK/getct"; LOCKD="$WORK/lock.d"
FAKE_KEY="sk-FAKESERVERKEY0000000000"; ALT_KEY="sk-ALTSUMIKEY000000000000"; PLACEHOLDER="PENDING-PROVISION"
SRV_PID=""
trap '[ -n "$SRV_PID" ] && kill "$SRV_PID" 2>/dev/null; rm -rf "$WORK"' EXIT
FAILS=0
fail(){ printf '    \033[31mFAIL\033[0m: %s\n' "$*"; FAILS=$((FAILS+1)); }
ok(){   printf '    ok: %s\n' "$*"; }

# ---------------- fake LiteLLM server (token hash + /v1/models identity) -----------------
cat > "$WORK/server.py" <<'PY'
import os, json, hashlib
from http.server import BaseHTTPRequestHandler, HTTPServer
from urllib.parse import urlparse, parse_qs
FAKE_KEY="sk-FAKESERVERKEY0000000000"; ALT_KEY="sk-ALTSUMIKEY000000000000"
def kh(k): return hashlib.sha256(k.encode()).hexdigest()
ALIASES={}; KEYS={ALT_KEY:{"alias":"some-other-sumi","models":["sumi"]}}   # a DIFFERENT sumi-scoped key (B3)
if os.environ.get("FAKE_PREEXIST_ALIAS"):
    ALIASES["sumi-voice-worker-v2"]={"models":["sumi"],"token":kh(FAKE_KEY)}
MINT=os.environ.get("FAKE_MINT_MODE","ok"); AUTH_FAIL=bool(os.environ.get("FAKE_AUTH_FAIL")); MODELS_BAD=bool(os.environ.get("FAKE_MODELS_BAD"))
def bearer(h):
    a=h.get("Authorization") or ""; return a[7:] if a.startswith("Bearer ") else ""
class H(BaseHTTPRequestHandler):
    def log_message(self,*a): pass
    def _s(self,c,o):
        b=json.dumps(o).encode(); self.send_response(c)
        self.send_header("Content-Type","application/json"); self.send_header("Content-Length",str(len(b))); self.end_headers(); self.wfile.write(b)
    def do_POST(self):
        n=int(self.headers.get("Content-Length") or 0); raw=self.rfile.read(n)
        try: p=json.loads(raw or b"{}")
        except Exception: p={}
        path=urlparse(self.path).path
        if path=="/key/generate":
            if MINT=="non2xx": return self._s(500,{"error":"boom"})
            alias=p.get("key_alias"); models=p.get("models") or []; scoped=["gpt-4"] if MODELS_BAD else models
            ALIASES[alias]={"models":scoped}
            if MINT=="commitdrop":
                try: self.connection.close()
                except Exception: pass
                return
            if MINT=="malformed": return self._s(200,{"key":None})
            ALIASES[alias]["token"]=kh(FAKE_KEY); KEYS[FAKE_KEY]={"alias":alias,"models":scoped}
            return self._s(200,{"key":FAKE_KEY,"key_alias":alias})
        if path=="/key/delete":
            for a in (p.get("key_aliases") or []):
                ALIASES.pop(a,None)
                for k in [k for k,v in KEYS.items() if v["alias"]==a]: KEYS.pop(k,None)
            return self._s(200,{"deleted":True})
        return self._s(404,{"error":"nope"})
    def do_GET(self):
        u=urlparse(self.path); q=parse_qs(u.query)
        if u.path=="/key/list":                                  # C1 positive-enumeration primitive
            if os.environ.get("FAKE_LIST_404"): return self._s(404,{"error":"Not Found"})   # generic route 404
            if AUTH_FAIL: return self._s(401,{"error":"Authentication Error"})
            mm=os.environ.get("FAKE_LIST_MALFORMED")             # D2 malformed-200 controls
            if mm=="false":       return self._s(200,{"total_count":False,"keys":[]})           # bool-is-int
            if mm=="neg":         return self._s(200,{"total_count":-1,"keys":[]})              # negative count
            if mm=="missingkeys": return self._s(200,{"total_count":0})                         # no keys field
            if mm=="tc0nonempty": return self._s(200,{"total_count":0,"keys":[{"key_alias":"x"}]})  # disagreement
            alias=(q.get("key_alias") or [""])[0]; present = alias in ALIASES
            return self._s(200,{"keys":([{"key_alias":alias}] if present else []),
                                "total_count":(1 if present else 0),"current_page":1,"total_pages":1})
        if u.path=="/key/info":
            if AUTH_FAIL: return self._s(401,{"error":"Authentication Error"})
            alias=(q.get("key_alias") or [""])[0]; ent=ALIASES.get(alias)
            if ent: return self._s(200,{"info":{"key_alias":alias,"models":ent["models"],"token":ent.get("token","")}})
            return self._s(404,{"error":"key not found"})
        if u.path=="/v1/models":
            ent=KEYS.get(bearer(self.headers))
            if not ent: return self._s(401,{"error":"invalid api key"})
            return self._s(200,{"object":"list","data":[{"id":m,"object":"model"} for m in ent["models"]]})
        return self._s(404,{"error":"nope"})
srv=HTTPServer(("127.0.0.1",0),H)
import sys; sys.stdout.write(str(srv.server_address[1])+"\n"); sys.stdout.flush(); srv.serve_forever()
PY

# ---------------- fake docker: real embedded Python; fail-on-demand -----------------------
cat > "$MOCKBIN/docker" <<'EOF'
#!/usr/bin/env bash
if [ "${1:-}" = "exec" ]; then
  n="$(cat "$MOCK_DOCKERCT" 2>/dev/null || echo 0)"; n=$((n+1)); echo "$n" > "$MOCK_DOCKERCT"
  if [ "${MOCK_DOCKER_FAIL:-0}" = 1 ] || [ "$n" -gt "${MOCK_DOCKER_OK_COUNT:-9999}" ]; then
    echo "Error response from daemon: container not running" >&2; exit 125
  fi
  shift 2
  exec env LITELLM_MASTER_KEY="fake-master-key" "$@"
fi
echo "unexpected docker $*" >&2; exit 99
EOF

# ---------------- fake op (stateful: id<TAB>title<TAB>correlation<TAB>credential) --------
cat > "$MOCKBIN/op" <<'EOF'
#!/usr/bin/env bash
rec(){ echo "$1" >> "$MOCK_RECEIPT"; }
# C3/D3: force a later lock-release rmdir to fail by leaving a file in the lock dir on the
# first op call after the lock is acquired (works for both success and preflight-abort paths).
[ "${MOCK_STALE_LOCK:-0}" = 1 ] && [ -n "${MOCK_LOCKDIR:-}" ] && [ -d "$MOCK_LOCKDIR" ] && : > "$MOCK_LOCKDIR/sentinel"
case "${1:-}" in
  item)
    case "${2:-}" in
      list)
        lc="$(cat "$MOCK_LISTCT" 2>/dev/null || echo 0)"; lc=$((lc+1)); echo "$lc" > "$MOCK_LISTCT"
        if [ "$lc" -gt "${MOCK_OP_LIST_OK_COUNT:-999}" ]; then echo "could not resolve host: op.example: no such host" >&2; exit 1; fi
        if [ -n "${MOCK_OP_LIST_SHAPE:-}" ] && [ "$lc" -gt "${MOCK_OP_LIST_SHAPE_AFTER:-0}" ]; then   # F1 malformed-success
          case "$MOCK_OP_LIST_SHAPE" in
            emptyobj) echo '{}' ;; null) echo 'null' ;; number) echo '42' ;;
            arraynull) echo '[null]' ;; notitle) echo '[{"id":"x"}]' ;; emptyid) echo '[{"id":"","title":"sumi-voice-litellm-api-key"}]' ;; *) echo '{}' ;;
          esac
          exit 0
        fi
        awk -F'\t' 'length($0){printf "%s%s",(NR>1?"\n":""),$0}' "$MOCK_OPSTATE" 2>/dev/null \
          | jq -R -s 'split("\n")|map(select(length>0))|map(split("\t"))|map({id:.[0],title:.[1]})' ;;
      get)
        gc="$(cat "$MOCK_GETCT" 2>/dev/null || echo 0)"; gc=$((gc+1)); echo "$gc" > "$MOCK_GETCT"
        if [ -n "${MOCK_OP_GET_SHAPE:-}" ] && [ "$gc" -gt "${MOCK_OP_GET_SHAPE_AFTER:-0}" ]; then         # F1 malformed item-get
          case "$MOCK_OP_GET_SHAPE" in
            emptyobj) echo '{}' ;; null) echo 'null' ;; nofields) echo '{"id":"MOCKITEM123","title":"x"}' ;; wrongid) echo '{"id":"WRONGITEM","title":"x","fields":[{"id":"credential","value":"y"}]}' ;; fieldnull) echo '{"id":"MOCKITEM123","title":"x","fields":[null]}' ;; *) echo '{}' ;;
          esac
          exit 0
        fi
        row="$(awk -F'\t' -v id="$3" '$1==id{print;exit}' "$MOCK_OPSTATE" 2>/dev/null)"
        [ -n "$row" ] || { echo "\"$3\" isn't an item" >&2; exit 1; }
        IFS=$'\t' read -r rid rt rc rcred <<<"$row"
        jq -n --arg id "$rid" --arg t "$rt" --arg c "$rc" --arg cred "$rcred" \
          '{id:$id,title:$t,category:"API_CREDENTIAL",fields:[{id:"credential",type:"CONCEALED",value:$cred},{id:"provision_run",type:"STRING",value:$c}]}' ;;
      create)
        in="$(cat)"; rec ITEM_CREATE
        echo "$in" | jq -e 'any(.fields[]?;.id=="credential")' >/dev/null 2>&1 || { echo "invalid template" >&2; exit 1; }
        t="$(echo "$in"|jq -r '.title')"; c="$(echo "$in"|jq -r '(.fields[]|select(.id=="provision_run")|.value)//""')"; cred="$(echo "$in"|jq -r '(.fields[]|select(.id=="credential")|.value)//""')"
        case "${MOCK_STORE:-ok}" in
          fail)       rec ITEM_CREATE_FAIL; exit 1 ;;
          commitdrop) printf 'MOCKITEM123\t%s\t%s\t%s\n' "$t" "$c" "$cred" >> "$MOCK_OPSTATE"; exit 1 ;;
          *)          printf 'MOCKITEM123\t%s\t%s\t%s\n' "$t" "$c" "$cred" >> "$MOCK_OPSTATE"; echo '{"id":"MOCKITEM123"}'; exit 0 ;;
        esac ;;
      edit)
        in="$(cat)"; echo "$in" | jq -e 'any(.fields[]?;.id=="credential")' >/dev/null 2>&1 || { echo "invalid/empty edit template" >&2; exit 1; }
        printf '%s' "$in" > "$MOCK_EDITCAP"; nc="$(echo "$in"|jq -r '(.fields[]|select(.id=="credential")|.value)//""')"
        case "${MOCK_EDIT:-ok}" in
          fail)  rec ITEM_EDIT_FAIL; exit 1 ;;
          stale) rec ITEM_EDIT; exit 0 ;;
          *)     awk -F'\t' -v OFS='\t' -v id="$3" -v nc="$nc" '$1==id{$4=nc}1' "$MOCK_OPSTATE" > "$MOCK_OPSTATE.t" && mv "$MOCK_OPSTATE.t" "$MOCK_OPSTATE"; rec ITEM_EDIT; exit 0 ;;
        esac ;;
      delete)
        rec "DELETE_ITEM($3)"; awk -F'\t' -v id="$3" '$1!=id' "$MOCK_OPSTATE" > "$MOCK_OPSTATE.t" 2>/dev/null && mv "$MOCK_OPSTATE.t" "$MOCK_OPSTATE"; exit 0 ;;
      *) echo "unexpected op item $2" >&2; exit 90 ;;
    esac ;;
  read) [ "${MOCK_RESOLVE:-ok}" = ok ] && exit 0 || exit 1 ;;
  run)
    # Parse --env-file and inject ONE fake resolved value PER op:// ref it names (D1): SUMI
    # gets the actually-stored credential; every OTHER ref gets a distinct LEAKED-<name> value
    # so a broad template is VISIBLE to verify-C's least-privilege sentinel. A Sumi-only
    # template therefore injects only SUMI; the broad shared template injects eight extras.
    shift; tpl=""
    while [ $# -gt 0 ] && [ "$1" != "--" ]; do
      case "$1" in --env-file=*) tpl="${1#--env-file=}" ;; --env-file) shift; tpl="${1:-}" ;; esac
      shift
    done
    [ "${1:-}" = "--" ] && shift
    stored="$(awk -F'\t' '$1=="MOCKITEM123"{print $4;exit}' "$MOCK_OPSTATE" 2>/dev/null)"
    case "${MOCK_INJECT:-ok}" in empty) stored="" ;; wrongkey) stored="sk-ALTSUMIKEY000000000000" ;; esac
    envargs=()
    if [ -n "$tpl" ] && [ -f "$tpl" ]; then
      while IFS= read -r line; do
        case "$line" in
          \#*|"") : ;;
          *=op://*) name="${line%%=*}"
                    if [ "$name" = "SUMI_LLM_API_KEY" ]; then envargs+=("$name=$stored"); else envargs+=("$name=LEAKED-$name"); fi ;;
        esac
      done < "$tpl"
    fi
    [ "${MOCK_INJECT_EXTRA:-0}" = 1 ] && envargs+=("OPENAI_API_KEY=LEAKED-EXTRA")   # D1 negative control
    env "${envargs[@]}" "$@" ;;
  *) echo "unexpected op $*" >&2; exit 91 ;;
esac
EOF
chmod +x "$MOCKBIN/docker" "$MOCKBIN/op"

PORT=0
start_server(){ env "$@" python3 "$WORK/server.py" > "$WORK/port" 2>"$WORK/srv.log" & SRV_PID=$!
  for _ in $(seq 1 50); do [ -s "$WORK/port" ] && break; sleep 0.05; done
  PORT="$(cat "$WORK/port" 2>/dev/null || true)"; [ -n "$PORT" ] || { echo "server failed"; cat "$WORK/srv.log"; exit 2; }; }
stop_server(){ [ -n "$SRV_PID" ] && kill "$SRV_PID" 2>/dev/null; wait "$SRV_PID" 2>/dev/null; SRV_PID=""; }

OUT=""; RC=0
run_helper(){  # $@ = MOCK_* env
  : > "$RECEIPT"; : > "$OPSTATE"; : > "$EDITCAP"; : > "$LISTCT"; : > "$DOCKERCT"; : > "$GETCT"; rm -rf "$LOCKD"
  OUT="$( cd "$REPO" && env PATH="$MOCKBIN:$PATH" LLM_URL="http://127.0.0.1:$PORT" OP_BIN=op \
        PROVISION_TEST_MODE=1 PROVISION_CORRELATION="test-corr-XYZ" PROVISION_LOCKDIR="$LOCKD" \
        MOCK_RECEIPT="$RECEIPT" MOCK_OPSTATE="$OPSTATE" MOCK_EDITCAP="$EDITCAP" MOCK_LISTCT="$LISTCT" MOCK_DOCKERCT="$DOCKERCT" MOCK_GETCT="$GETCT" MOCK_LOCKDIR="$LOCKD" \
        "$@" bash "$SCRIPT" 2>&1 )" && RC=0 || RC=$?; }
actions(){ paste -sd, "$RECEIPT" 2>/dev/null || true; }
has(){ grep -qF "$1" "$RECEIPT" 2>/dev/null; }
assert_rc(){ [ "$RC" = "$1" ] && ok "rc=$RC" || fail "rc expected $1 got $RC  (actions: $(actions))"; }
assert_has(){ has "$1" && ok "action $1 present" || fail "action $1 MISSING (actions: $(actions))"; }
assert_absent(){ has "$1" && fail "action $1 present but should be ABSENT (actions: $(actions))" || ok "action $1 absent"; }
assert_no_leak(){ for kk in "$FAKE_KEY" "$ALT_KEY"; do printf '%s' "$OUT" | grep -qF "$kk" && { fail "KEY LEAKED into helper output"; return; }; done; ok "no key in helper output"; }
assert_state_empty(){ [ -s "$OPSTATE" ] && fail "1P state not empty after rollback: $(cat "$OPSTATE")" || ok "1P item state empty after rollback"; }
hdr(){ printf '\n=== %s\n' "$*"; }

echo "# static checks"
bash -n "$SCRIPT" && ok "bash -n clean" || fail "bash -n"
command -v shellcheck >/dev/null 2>&1 && { shellcheck -S warning "$SCRIPT" >/dev/null 2>&1 && ok "shellcheck clean" || { echo "  shellcheck findings:"; shellcheck -S warning "$SCRIPT" || true; }; }

echo; echo "# scenarios (real embedded Python vs local fake server; stateful fake op; fail-on-demand docker)"

hdr "S1 preflight: 1P item present -> ABORT"
start_server FAKE_MINT_MODE=ok
: > "$RECEIPT"; : > "$EDITCAP"; : > "$LISTCT"; : > "$DOCKERCT"; rmdir "$LOCKD" 2>/dev/null || true
printf 'SEED\tsumi-voice-litellm-api-key\tseed\tseedcred\n' > "$OPSTATE"
OUT="$( cd "$REPO" && env PATH="$MOCKBIN:$PATH" LLM_URL="http://127.0.0.1:$PORT" OP_BIN=op PROVISION_TEST_MODE=1 PROVISION_CORRELATION=test-corr-XYZ PROVISION_LOCKDIR="$LOCKD" \
      MOCK_RECEIPT="$RECEIPT" MOCK_OPSTATE="$OPSTATE" MOCK_EDITCAP="$EDITCAP" MOCK_LISTCT="$LISTCT" MOCK_DOCKERCT="$DOCKERCT" bash "$SCRIPT" 2>&1 )" && RC=0 || RC=$?
assert_rc 3; assert_absent ITEM_CREATE
stop_server

hdr "S2 [F2] preflight: op item list fails (host-not-found) -> UNKNOWN -> abort"
start_server FAKE_MINT_MODE=ok; run_helper MOCK_OP_LIST_OK_COUNT=0
assert_rc 3; assert_absent ITEM_CREATE
stop_server

hdr "S3 preflight: LiteLLM alias present -> ABORT"
start_server FAKE_MINT_MODE=ok FAKE_PREEXIST_ALIAS=1; run_helper
assert_rc 3; assert_absent ITEM_CREATE
stop_server

hdr "S4 [F1] preflight: alias 401 -> UNKNOWN -> abort"
start_server FAKE_MINT_MODE=ok FAKE_AUTH_FAIL=1; run_helper
assert_rc 3; assert_absent ITEM_CREATE
stop_server

hdr "S5 [B1] preflight: docker exec FAILS -> UNKNOWN -> abort (no fall-through fail-open)"
start_server FAKE_MINT_MODE=ok; run_helper MOCK_DOCKER_FAIL=1
assert_rc 3; assert_absent ITEM_CREATE
stop_server

hdr "S6 [B4] exclusive lock already held -> ABORT rc=5, NO mutation"
start_server FAKE_MINT_MODE=ok
: > "$RECEIPT"; : > "$OPSTATE"; : > "$EDITCAP"; : > "$LISTCT"; : > "$DOCKERCT"; rmdir "$LOCKD" 2>/dev/null || true; mkdir "$LOCKD"
OUT="$( cd "$REPO" && env PATH="$MOCKBIN:$PATH" LLM_URL="http://127.0.0.1:$PORT" OP_BIN=op PROVISION_TEST_MODE=1 PROVISION_CORRELATION=test-corr-XYZ PROVISION_LOCKDIR="$LOCKD" \
      MOCK_RECEIPT="$RECEIPT" MOCK_OPSTATE="$OPSTATE" MOCK_EDITCAP="$EDITCAP" MOCK_LISTCT="$LISTCT" MOCK_DOCKERCT="$DOCKERCT" bash "$SCRIPT" 2>&1 )" && RC=0 || RC=$?
assert_rc 5; assert_absent ITEM_CREATE; assert_state_empty
[ -d "$LOCKD" ] && ok "pre-held lock NOT removed by the refused run" || fail "refused run removed someone else's lock"
rmdir "$LOCKD" 2>/dev/null || true
stop_server

hdr "S7 placeholder create fails -> no mint, nothing committed"
start_server FAKE_MINT_MODE=ok; run_helper MOCK_STORE=fail
assert_rc 1; assert_has ITEM_CREATE_FAIL; assert_absent ITEM_EDIT; assert_no_leak; assert_state_empty
stop_server

hdr "S8 mint non-2xx -> rollback VERIFIED"
start_server FAKE_MINT_MODE=non2xx; run_helper
assert_rc 1; assert_has "DELETE_ITEM(MOCKITEM123)"; assert_absent ITEM_EDIT; assert_no_leak; assert_state_empty
stop_server

hdr "S9 mint malformed -> self-revoke, delete, VERIFIED"
start_server FAKE_MINT_MODE=malformed; run_helper
assert_rc 1; assert_has "DELETE_ITEM(MOCKITEM123)"; assert_no_leak; assert_state_empty
stop_server

hdr "S10 mint commit-plus-response-loss -> VERIFIED"
start_server FAKE_MINT_MODE=commitdrop; run_helper
assert_rc 1; assert_has "DELETE_ITEM(MOCKITEM123)"; assert_no_leak; assert_state_empty
stop_server

hdr "S11 [F3] CREATE commit-drop (empty ITEM_ID) -> trap finds orphan by CORRELATION -> VERIFIED"
start_server FAKE_MINT_MODE=ok; run_helper MOCK_STORE=commitdrop
assert_rc 1; assert_has ITEM_CREATE; assert_has "DELETE_ITEM(MOCKITEM123)"; assert_no_leak; assert_state_empty
stop_server

hdr "S12 op item edit fails -> minted, rolled back, NOT leaked"
start_server FAKE_MINT_MODE=ok; run_helper MOCK_EDIT=fail
assert_rc 1; assert_has ITEM_EDIT_FAIL; assert_has "DELETE_ITEM(MOCKITEM123)"; assert_no_leak; assert_state_empty
stop_server

hdr "S13 verify A (op read) fails -> rollback VERIFIED"
start_server FAKE_MINT_MODE=ok; run_helper MOCK_RESOLVE=fail
assert_rc 1; assert_has "DELETE_ITEM(MOCKITEM123)"; assert_no_leak; assert_state_empty
stop_server

hdr "S14 [F4] verify B (alias/models mismatch) -> rollback VERIFIED"
start_server FAKE_MINT_MODE=ok FAKE_MODELS_BAD=1; run_helper
assert_rc 1; assert_has "DELETE_ITEM(MOCKITEM123)"; assert_no_leak; assert_state_empty
stop_server

hdr "S15 [F5] stale placeholder (stored value NOT replaced) -> verify C catches -> rollback"
start_server FAKE_MINT_MODE=ok; run_helper MOCK_EDIT=stale
assert_rc 1; assert_has "DELETE_ITEM(MOCKITEM123)"; assert_no_leak; assert_state_empty
stop_server

hdr "S16 [B3] impostor: RIGHT scope, WRONG identity (hash != alias token) -> verify C catches -> rollback"
start_server FAKE_MINT_MODE=ok; run_helper MOCK_INJECT=wrongkey
assert_rc 1; assert_has "DELETE_ITEM(MOCKITEM123)"; assert_no_leak; assert_state_empty
printf '%s' "$OUT" | grep -qF "verify C" && ok "failure attributed to verify C (identity correlation)" || fail "verify C did not fire on impostor"
stop_server

hdr "S17 verify C: injection empty -> rollback VERIFIED"
start_server FAKE_MINT_MODE=ok; run_helper MOCK_INJECT=empty
assert_rc 1; assert_has "DELETE_ITEM(MOCKITEM123)"; assert_no_leak; assert_state_empty
stop_server

hdr "S18 SUCCESS -> create+edit, no delete, key reached edit stdin, CORRELATED to alias & scoped [sumi], no leak"
start_server FAKE_MINT_MODE=ok; run_helper
assert_rc 0; assert_has ITEM_CREATE; assert_has ITEM_EDIT; assert_absent "DELETE_ITEM(MOCKITEM123)"; assert_no_leak
grep -qF "$FAKE_KEY" "$EDITCAP" && ok "server-minted key reached op item edit stdin (real Python, end-to-end)" || fail "key did not reach edit stdin"
grep -qF "$PLACEHOLDER" "$EDITCAP" && fail "edit still carried placeholder" || ok "edit replaced placeholder"
stored="$(awk -F'\t' '$1=="MOCKITEM123"{print $4}' "$OPSTATE")"; [ "$stored" = "$FAKE_KEY" ] && ok "stored credential IS the minted key (verify-C hash-correlated it to alias v2)" || fail "stored wrong: $stored"
stop_server

hdr "S19 [B2] docker fails DURING cleanup -> CLEANUP-INCOMPLETE rc=4 (no silent early exit)"
start_server FAKE_MINT_MODE=ok; run_helper MOCK_RESOLVE=fail MOCK_DOCKER_OK_COUNT=3
assert_rc 4
printf '%s' "$OUT" | grep -qF "CLEANUP-INCOMPLETE" && ok "distinct CLEANUP-INCOMPLETE receipt" || fail "no CLEANUP-INCOMPLETE receipt"
stop_server

hdr "S20 [G5] cleanup enumeration fails -> CLEANUP-INCOMPLETE rc=4"
start_server FAKE_MINT_MODE=non2xx; run_helper MOCK_OP_LIST_OK_COUNT=1
assert_rc 4
printf '%s' "$OUT" | grep -qF "CLEANUP-INCOMPLETE" && ok "distinct CLEANUP-INCOMPLETE receipt" || fail "no CLEANUP-INCOMPLETE receipt"
stop_server

hdr "S21 [C1] preflight: alias endpoint returns a GENERIC 404 (not an alias-not-found) -> UNKNOWN -> abort"
start_server FAKE_MINT_MODE=ok FAKE_LIST_404=1; run_helper
assert_rc 3; assert_absent ITEM_CREATE   # a bare/wrong-path 404 must NOT read as 'alias absent'
stop_server

hdr "S22 [C2] production-mode lock override IGNORED -> canonical path used (pre-held) -> abort rc5, decoy untouched"
start_server FAKE_MINT_MODE=ok
CANON="/tmp/sumi-voice-litellm-provision.lock.d"; DECOY="$WORK/decoy.lock.d"
rm -rf "$CANON" "$DECOY"; mkdir "$CANON"                     # pre-hold the CANONICAL path
: > "$RECEIPT"; : > "$OPSTATE"; : > "$EDITCAP"; : > "$LISTCT"; : > "$DOCKERCT"
# NOTE: no PROVISION_TEST_MODE -> override must be ignored; PROVISION_LOCKDIR points at the decoy.
OUT="$( cd "$REPO" && env PATH="$MOCKBIN:$PATH" LLM_URL="http://127.0.0.1:$PORT" OP_BIN=op \
      PROVISION_LOCKDIR="$DECOY" MOCK_RECEIPT="$RECEIPT" MOCK_OPSTATE="$OPSTATE" MOCK_EDITCAP="$EDITCAP" \
      MOCK_LISTCT="$LISTCT" MOCK_DOCKERCT="$DOCKERCT" bash "$SCRIPT" 2>&1 )" && RC=0 || RC=$?
assert_rc 5; assert_absent ITEM_CREATE
[ -d "$DECOY" ] && fail "decoy lock dir was created -> override was honored in prod mode" || ok "decoy untouched -> PROVISION_LOCKDIR ignored in production"
[ -d "$CANON" ] && ok "canonical lock left intact by refused run" || fail "refused run removed the canonical lock"
rm -rf "$CANON" "$DECOY"
stop_server

hdr "S23 [C3] success but lock release FAILS -> rc6 LOCK-RELEASE-FAILED, credentials KEPT (no rollback)"
start_server FAKE_MINT_MODE=ok; run_helper MOCK_STALE_LOCK=1
assert_rc 6
printf '%s' "$OUT" | grep -qF "LOCK-RELEASE-FAILED" && ok "distinct LOCK-RELEASE-FAILED receipt" || fail "no LOCK-RELEASE-FAILED receipt"
assert_has ITEM_EDIT; assert_absent "DELETE_ITEM(MOCKITEM123)"   # credentials provisioned, NOT rolled back
stored="$(awk -F'\t' '$1=="MOCKITEM123"{print $4}' "$OPSTATE")"; [ "$stored" = "$FAKE_KEY" ] && ok "stored credential intact (valid success, only the lock is stale)" || fail "credential lost: $stored"
rm -rf "$LOCKD"
stop_server

hdr "S24 [E2] BROAD shared template -> pre-mutation template validation fails -> abort rc8, NO mutation"
start_server FAKE_MINT_MODE=ok; run_helper PROVISION_VERIFY_TPL=config/livekit.env.tpl
assert_rc 8; assert_absent ITEM_CREATE
printf '%s' "$OUT" | grep -qF "not Sumi-only" && ok "rejected BEFORE any mutation (template-validate), not merely at verify-C" || fail "no template-validate rejection"
stop_server

hdr "S25 [D2] malformed 200: total_count=false (bool-is-int) -> UNKNOWN -> abort, no create"
start_server FAKE_MINT_MODE=ok FAKE_LIST_MALFORMED=false; run_helper
assert_rc 3; assert_absent ITEM_CREATE
stop_server

hdr "S26 [D2] malformed 200: total_count=-1 -> UNKNOWN -> abort, no create"
start_server FAKE_MINT_MODE=ok FAKE_LIST_MALFORMED=neg; run_helper
assert_rc 3; assert_absent ITEM_CREATE
stop_server

hdr "S27 [D2] malformed 200: keys field missing -> UNKNOWN -> abort, no create"
start_server FAKE_MINT_MODE=ok FAKE_LIST_MALFORMED=missingkeys; run_helper
assert_rc 3; assert_absent ITEM_CREATE
stop_server

hdr "S28 [D2] malformed 200: total_count=0 but keys nonempty (disagreement) -> UNKNOWN -> abort"
start_server FAKE_MINT_MODE=ok FAKE_LIST_MALFORMED=tc0nonempty; run_helper
assert_rc 3; assert_absent ITEM_CREATE
stop_server

hdr "S29 [D3] preflight abort AFTER lock acquired + forced release failure -> rc7, NO mutation, truthful receipt"
start_server FAKE_MINT_MODE=ok
: > "$RECEIPT"; : > "$EDITCAP"; : > "$LISTCT"; : > "$DOCKERCT"; rm -rf "$LOCKD"
printf 'SEED\tsumi-voice-litellm-api-key\tseed\tseedcred\n' > "$OPSTATE"   # item present -> preflight abort
OUT="$( cd "$REPO" && env PATH="$MOCKBIN:$PATH" LLM_URL="http://127.0.0.1:$PORT" OP_BIN=op \
      PROVISION_TEST_MODE=1 PROVISION_CORRELATION=test-corr-XYZ PROVISION_LOCKDIR="$LOCKD" MOCK_LOCKDIR="$LOCKD" MOCK_STALE_LOCK=1 \
      MOCK_RECEIPT="$RECEIPT" MOCK_OPSTATE="$OPSTATE" MOCK_EDITCAP="$EDITCAP" MOCK_LISTCT="$LISTCT" MOCK_DOCKERCT="$DOCKERCT" bash "$SCRIPT" 2>&1 )" && RC=0 || RC=$?
assert_rc 7   # abort-with-stale-lock, explicitly NOT provisioned rc6
assert_absent ITEM_CREATE; assert_absent ITEM_EDIT; assert_absent "DELETE_ITEM(MOCKITEM123)"
printf '%s' "$OUT" | grep -qF "LOCK-STALE-AFTER-ABORT" && ok "truthful no-credentials receipt" || fail "no LOCK-STALE-AFTER-ABORT receipt"
printf '%s' "$OUT" | grep -qF "LOCK-RELEASE-FAILED:" && fail "masqueraded as provisioned rc6" || ok "did NOT emit the provisioned-success receipt"
rm -rf "$LOCKD"
stop_server

hdr "S30 [E2] template with an UNKNOWN FUTURE op-backed ref (SUMI + FOO_FUTURE) -> abort rc8, no mutation"
start_server FAKE_MINT_MODE=ok
{ printf 'SUMI_LLM_API_KEY=op://Harem World/sumi-voice-litellm-api-key/credential\n'; printf 'FOO_FUTURE=op://Harem World/whatever/credential\n'; } > "$WORK/tpl-future.env"
run_helper PROVISION_VERIFY_TPL="$WORK/tpl-future.env"
assert_rc 8; assert_absent ITEM_CREATE   # exact-set validation catches names NOT on any blocklist
stop_server

hdr "S31 [E2] template MISSING the Sumi ref -> abort rc8, no mutation"
start_server FAKE_MINT_MODE=ok
printf '# only a comment, no active ref\n' > "$WORK/tpl-missing.env"
run_helper PROVISION_VERIFY_TPL="$WORK/tpl-missing.env"
assert_rc 8; assert_absent ITEM_CREATE
stop_server

hdr "S32 [E2] template with a DUPLICATE Sumi ref -> abort rc8, no mutation"
start_server FAKE_MINT_MODE=ok
{ printf 'SUMI_LLM_API_KEY=op://Harem World/sumi-voice-litellm-api-key/credential\n'; printf 'SUMI_LLM_API_KEY=op://Harem World/sumi-voice-litellm-api-key/credential\n'; } > "$WORK/tpl-dup.env"
run_helper PROVISION_VERIFY_TPL="$WORK/tpl-dup.env"
assert_rc 8; assert_absent ITEM_CREATE
stop_server

hdr "S33 [E2] template with a WRONG Sumi op URI -> abort rc8, no mutation"
start_server FAKE_MINT_MODE=ok
printf 'SUMI_LLM_API_KEY=op://Harem World/some-other-item/credential\n' > "$WORK/tpl-wrong.env"
run_helper PROVISION_VERIFY_TPL="$WORK/tpl-wrong.env"
assert_rc 8; assert_absent ITEM_CREATE
stop_server

hdr "S34 [E2] EXACT production Sumi-only template passes validation -> proceeds to success"
start_server FAKE_MINT_MODE=ok; run_helper PROVISION_VERIFY_TPL=config/sumi-llm-key.env.tpl
assert_rc 0; assert_has ITEM_EDIT; assert_no_leak   # the real template is accepted; provisioning completes
stop_server

hdr "S35 [D1 defense-in-depth] valid template BUT env polluted with an extra forbidden secret -> runtime sentinel fires -> rollback"
start_server FAKE_MINT_MODE=ok; run_helper MOCK_INJECT_EXTRA=1
assert_rc 1; assert_has "DELETE_ITEM(MOCKITEM123)"; assert_no_leak; assert_state_empty
printf '%s' "$OUT" | grep -qF "verify C" && ok "runtime sentinel caught out-of-template injection (belt AND braces)" || fail "runtime sentinel did not fire"
stop_server

hdr "S36 [E2] canonical launch references the Sumi-only template (doc assertion)"
grep -qF "op run --env-file=config/sumi-llm-key.env.tpl" docs/SLICE-6-LIVEKIT-PLANE.md && ok "SLICE-6 launch op-runs config/sumi-llm-key.env.tpl" || fail "SLICE-6 launch does not reference the Sumi-only template"
grep -qE 'op run --env-file=config/livekit\.env\.tpl' docs/SLICE-6-LIVEKIT-PLANE.md && fail "SLICE-6 launch still op-runs the shared livekit.env.tpl" || ok "SLICE-6 launch no longer op-runs the shared template"
grep -qE '=op://' config/livekit.env.tpl && ! grep -qi sumi config/livekit.env.tpl && ok "shared livekit.env.tpl carries NO Sumi ref (E1)" || fail "shared template still references Sumi"

# --- F1: 1Password malformed-but-successful JSON must read UNKNOWN, never absent ---
for shape in emptyobj null number arraynull notitle; do
  hdr "S37.$shape [F1] PREFLIGHT op-item-list malformed-success ($shape) -> UNKNOWN -> abort rc3, no create"
  start_server FAKE_MINT_MODE=ok
  run_helper MOCK_OP_LIST_SHAPE="$shape" MOCK_OP_LIST_SHAPE_AFTER=0   # first (preflight) list is malformed
  assert_rc 3; assert_absent ITEM_CREATE
  stop_server
done

hdr "S38 [F1] CLEANUP op-item-list malformed-success -> return2 -> CLEANUP-INCOMPLETE rc4 (not verified)"
start_server FAKE_MINT_MODE=non2xx
# preflight list ok (AFTER=1), item created, mint fails -> cleanup list #2 is malformed
run_helper MOCK_OP_LIST_SHAPE=emptyobj MOCK_OP_LIST_SHAPE_AFTER=1
assert_rc 4
printf '%s' "$OUT" | grep -qF "CLEANUP-INCOMPLETE" && ok "cleanup did NOT claim verified on a malformed list" || fail "no CLEANUP-INCOMPLETE on malformed cleanup list"
stop_server

hdr "S39 [F1] CLEANUP item-get malformed body -> return2 -> CLEANUP-INCOMPLETE rc4 (not read as not-correlated)"
start_server FAKE_MINT_MODE=non2xx
# get #1 (placeholder readback) ok, get #2 (cleanup correlation) malformed -> not silently 'not correlated'
run_helper MOCK_OP_GET_SHAPE=nofields MOCK_OP_GET_SHAPE_AFTER=1
assert_rc 4
printf '%s' "$OUT" | grep -qF "CLEANUP-INCOMPLETE" && ok "cleanup did NOT claim verified on a malformed item-get" || fail "no CLEANUP-INCOMPLETE on malformed item-get"
stop_server

hdr "S39b [G1] CLEANUP item-get returns WRONG id (not the requested id) -> return2 -> rc4, not verified"
start_server FAKE_MINT_MODE=non2xx
run_helper MOCK_OP_GET_SHAPE=wrongid MOCK_OP_GET_SHAPE_AFTER=1
assert_rc 4
printf '%s' "$OUT" | grep -qF "CLEANUP-INCOMPLETE" && ok "returned-id mismatch -> not read as 'not ours'" || fail "wrong returned id was tolerated"
stop_server

hdr "S39c [G1] CLEANUP item-get has a malformed field entry ([null]) -> return2 -> rc4, not verified"
start_server FAKE_MINT_MODE=non2xx
run_helper MOCK_OP_GET_SHAPE=fieldnull MOCK_OP_GET_SHAPE_AFTER=1
assert_rc 4
printf '%s' "$OUT" | grep -qF "CLEANUP-INCOMPLETE" && ok "malformed field entry -> not read as 'not correlated'" || fail "malformed fields entry was tolerated"
stop_server

hdr "S39d [G1] CLEANUP list has an EMPTY-STRING id -> strict list schema -> return2 -> rc4, not verified"
start_server FAKE_MINT_MODE=non2xx
run_helper MOCK_OP_LIST_SHAPE=emptyid MOCK_OP_LIST_SHAPE_AFTER=1
assert_rc 4
printf '%s' "$OUT" | grep -qF "CLEANUP-INCOMPLETE" && ok "empty-id list -> not a false-empty proof" || fail "empty-id list was tolerated as empty success"
stop_server

hdr "S40 [NEG-CONTROL] leak detector must FIRE on a planted key"
printf 'noise %s noise' "$FAKE_KEY" | grep -qF "$FAKE_KEY" && ok "leak detector fires (not a no-op)" || fail "leak detector is a NO-OP"

echo
if [ "$FAILS" = 0 ]; then printf '\033[32mALL SCENARIOS PASSED\033[0m (%s)\n' "$SCRIPT"; exit 0
else printf '\033[31m%s ASSERTION(S) FAILED\033[0m\n' "$FAILS"; exit 1; fi
