# Slice 4 — Sumi LLM output-cap safety repair (ARTIFACT v11 — post v10 second-read)

## 2026-07-27 live-turn amendment — bounded 256-token replies

The original 64-token ceiling below remains the historical fix for the
2026-07-23 uncapped-generation incident, but it is no longer the current worker
contract. A live requested story on 2026-07-27 reached exactly 64 completion
tokens and ended mid-clause. The provider and both TTS requests completed
normally; the worker then stored the truncated turn as though it were complete.

Current contract:

- `_LLM_MAX_TOKENS_CEILING = 256`; `SUMI_LLM_MAX_TOKENS` remains lower-only and
  still fails loud above the compiled ceiling, below 1, or when unparseable.
- A normally drained provider stream whose usage reaches the cap appends an
  explicit spoken/history notice: `I reached my reply limit before I could
  finish that. Ask me to continue.`
- No notice is emitted for below-cap completion, cancellation, consumer close,
  or provider error.
- Because usage arrives after streamed tokens, the notice cannot retract an
  already-spoken incomplete tail. Preventing that tail would require buffering
  the whole reply and is a separate latency experiment.
- 256 is a bounded first expansion, not a claim about the final long-form phone
  policy and not permission for unlimited generation.

The 2026-07-27 receipt is 74/74 Sumi tests passed with ruff clean. The original
64-token implementation and receipts below are retained as incident history;
they must not be read as the current live value.

**v11** closes Yua's v10 second-read: **G1** — the cleanup item-get schema (v10) required only
*some* string `id` + a `fields` array; it did not bind the returned `.id` to the requested id and
accepted malformed field entries, so `{id:WRONGITEM,fields:[]}` or `{id:MOCKITEM123,fields:[null]}`
passed and the real correlated item could be read as *not ours* → false "verified". The list
schema also accepted an empty-string id (skipped as a false-empty proof). Now: list entries need
**non-empty** string `id`+`title`; each item-get must have `.id` **exactly equal** to the requested
id, `fields` an array of well-formed objects (non-empty string id), and the correlation field a
non-empty string value — any mismatch is `return 2` (→ `CLEANUP_INCOMPLETE`, never verified).
**G2** — the v10 SLICE-6 retirement wording was unsafe: it said revoke `sumi-voice-worker-v2`
*after* acceptance, but v2 is the key the accepted worker **uses**. Corrected to a proper dual-key
lifecycle (revoke v2 only on the failure branch; keep it on success; retire the PRIOR key later
under separate authorization; verify prior alias metadata live at execution).

**v10** closed Yua's v9 second-read: **F1** — the malformed-successful-JSON hardening added to
`alias_state` (D2) was NOT carried to the 1Password surfaces. `op_item_state` (preflight) and
`_our_item_ids` (cleanup) still mapped `{}`/`null`/`42`/`[null]`/`[{"id":…}]` to *absent* /
*not-correlated*. Both now apply a **strict list schema** (top-level array; every entry an object
with string `id` AND string `title`) and a **strict item-get schema** (object with string `id`
and a `fields` array) before classifying — any malformed success is UNKNOWN (preflight abort) or
`return 2` (cleanup → `CLEANUP_INCOMPLETE`, never a false "verified"). **F2** — doc drift fixed:
the stale `24`-scenario count is now the run-derived count, and SLICE-6 names the canonical
`sumi-voice-worker-v2` alias with the `key_aliases` delete contract and a dual-key lifecycle
(corrected in v11 G2 — v2 is kept on success, revoked only on failure).

**v9 completed D1** against Yua's v8 second-read: **E1** — the shared `config/livekit.env.tpl`
still carried `SUMI_LLM_API_KEY` (I had *added* it there), so every generic op-run consumer
resolved Sumi's credential. The dedicated surface must REPLACE, not duplicate: the Sumi ref is
removed from the shared template (its diff is now zero against HEAD) and lives only in
`config/sumi-llm-key.env.tpl`. **E2** — the runtime `FORBIDDEN_ENV` blocklist only knew today's
eight names; an unknown *future* op-backed ref would slip past. The Sumi-only template is now
**validated before any mutation** to be EXACTLY one active assignment (the expected Sumi ref) —
any extra/duplicate/wrong/missing ref aborts (rc 8, no mutation), with the runtime sentinel kept
as defense-in-depth. Harness is now **47 scenarios**. (Prior header below retained for the D1–D4
and C/B/F history.)

Bounded fix for the 2026-07-23 uncapped-generation incident. The worker-side safety
cap (committed at `2d65dea`) is done and rehearsed; the secret-authority provisioning
below is revised through Yua's eighth read: **D1** (verify-C and the launch reused the
8-ref `livekit.env.tpl`, so `op run` resolved eight unrelated secrets into the verifier/CLI
process — now a **Sumi-only** template plus a least-privilege **sentinel** that fails closed
if any non-Sumi op-backed secret is present); **D2** (`isinstance(tc,int)` let
`total_count=false`/`-1` fall to *absent* since `bool` is an `int` — now `type(tc) is int`,
`tc==0` **and** `keys==[]` only, all else UNKNOWN); **D3** (a no-mutation preflight abort
borrowed `success=1` to suppress rollback, so a lock-release failure mislabeled it as
provisioned rc 6 — now a three-state `OUTCOME` with a distinct **rc 7** for abort-with-stale-
lock); **D4** (artifact identity bumped to v8). C1–C3, B1–B4, F1–F5 fixes retained.
**Nothing new is deployed.**
No inference, no live Momo/LiteLLM mutation, no 1Password write, no SIP, no
Tama/Shiori/shared-Hermes work. HEAD stays `2d65dea`; the provisioning scripts are
artifacts, uncommitted, and have only ever run against a local fake HTTP server.

## What the incident actually was (corrected, closed)

The Momo pressure came from a **legitimate** Sumi health-audit run from Sumi's own
machine (10.0.20.20) whose request inherited an **uncapped** custom-provider default
(toward 65536) on a memory-thin host. Attribution was closed **FALSE** — not Tama,
not Vesper, not the Sumi voice worker. But the risk class it surfaced — an uncapped
generation pinning a slot on a thin host — is real, and worth fixing for Sumi's own
voice path before a live call.

## The change (narrow, worker-side only)

`agents/sumi/src/agent.py`
- **Hard, lower-only cap (F1).** `_LLM_MAX_TOKENS_CEILING = 64` plus
  `_resolve_max_tokens()`: the env `SUMI_LLM_MAX_TOKENS` may only **lower** the cap
  (1..64). A value above the ceiling, non-numeric, or < 1 **fails loud** — a cap the
  environment can raise is not a cap. (The prior `int(os.environ.get(..., "64"))`
  could be lifted to 65536, defeating the fix — exactly F1.)
- **`build_llm(*, client=None)` factory (F1).** The single source of truth for the
  worker LLM, used by BOTH the entrypoint (`llm = build_llm()`) AND the tests, so the
  tests exercise the real construction/cap — not a test-local constant. `client`
  injects an openai client (e.g. httpx-mocked) for zero-network tests.
- Applies `max_completion_tokens=_resolve_max_tokens()` on every turn.

`agents/sumi/prompts/system.md`
- In-character phone-turn contract in "How you speak": one or two short sentences,
  never a monologue — brevity reads as Sumi, not a bolt-on.

`agents/sumi/tests/test_llm_safety.py` (rewritten)
- Exercises the real `build_llm()` path over a real `openai.AsyncClient` backed by an
  `httpx.MockTransport` — zero network.

## Test receipt (full suite 35 passed, ruff clean)

**F1 — real path, lower-only cap:**
- `test_outbound_request_capped_at_ceiling_by_default` — asserts the **serialized
  HTTP request body** carries `max_completion_tokens=64`. **PASS.**
- `test_env_override_may_only_lower_the_cap` — `=32` flows through to the wire. **PASS.**
- `test_override_above_ceiling_fails_loud` / `_non_numeric_` / `_zero_` — `build_llm`
  refuses to construct. **PASS (red-proofs).**
- **Discriminatory receipt (the old gap), now closed:** running the production path
  `SUMI_LLM_MAX_TOKENS=65536 build_llm()` → `RuntimeError: may only LOWER the 64-token
  safety ceiling`. The old test passed under this env because it built its own
  CAP=64; the new tests use `build_llm()`, so they cannot.

**F2 — real downstream HTTP closure:**
- `test_interrupt_closes_downstream_http_stream` — a real openai client over an httpx
  MockTransport whose byte stream records `aclose()`. Interrupting mid-turn
  (`LLMStream.aclose()`, what `AgentSession` triggers on disconnect) closes the
  **transport** stream. **PASS.** (Not a handwritten context-manager exit.)
- **Honest limitation kept:** this proves the worker/OpenAI-client closes the
  downstream HTTP stream. Whether the **LiteLLM proxy** then aborts the momo upstream
  on that close is proxy behavior NOT exercised here; the 64-token cap bounds the
  blast radius regardless.

## Requirement 3 — route ceiling: still NOT shipping one (honest)

Verified read-only: the running LiteLLM has **no enforcement surface**
(`callbacks: ["prometheus"]` only). A route's `litellm_params.max_tokens` is a
caller-**overridable default**, not a clamp — the "default dressed as a ceiling" to
reject. Real enforcement needs a LiteLLM pre-call guardrail / key-level clamp: shared-
infra code+deploy, out of this lane and gated. The enforced bound we ship is the
worker-side cap; the guardrail is filed as separate gated work.

## Deploy / proof / ROLLBACK plan (F3 v2 — preserve the OLD CONTAINER, no secrets to disk)

Executed only after re-review + go. Rollback restores a **registered + idle worker**
by keeping the exact prior container **intact** (via rename — not reconstructed from a
spec), and the cycle is **rehearsed before** the real deploy. No secrets are ever
written to disk: the prior container is preserved as-is, and receipts capture only
non-secret image ID / status / labels — never container env (which holds the scoped
LiteLLM bearer + `MUSUBI_V2_TOKEN_SUMI`).

1. **Rehearse the rename/restart cycle FIRST (prove rollback before deploying),** with
   no image change on the current worker:
   `docker stop voice-agent-sumi` → `docker rename voice-agent-sumi voice-agent-sumi-prev`
   → `docker rename voice-agent-sumi-prev voice-agent-sumi` → `docker start voice-agent-sumi`
   → confirm it re-registers as `phone-sumi` and sits idle. Only a proven cycle earns
   the deploy.
2. **Build the capped image under a UNIQUE immutable tag** (e.g.
   `voice-agent:sumi-<shortsha>`); shared `:latest` untouched.
3. **Deploy the replacement, leaving the old container INTACT:**
   `docker stop voice-agent-sumi` → `docker rename voice-agent-sumi voice-agent-sumi-prev`
   (kept, stopped) → start the replacement from the **canonical documented run command**
   with the **normal secret source** (scoped `sumi` key from its usual place, never a
   captured file), isolated on `voice_default`, idle explicit-dispatch. No SIP, no
   shared rebuild.
4. **One bounded synthetic turn under monitoring** (Shiori watching Momo). Accept iff:
   spoken turn short (≤ ~64 tokens, 1–2 sentences); `finish_reason=stop`; **and the
   safety gate — Momo shows no residual slot and no distress** (MemAvailable steady,
   PSI 0, no lingering llama-server slot on the sumi request).
5. **On ANY failure → restore the intact old container:**
   `docker rm -f voice-agent-sumi` (the replacement) →
   `docker rename voice-agent-sumi-prev voice-agent-sumi` → `docker start voice-agent-sumi`
   → prove registered + idle. Same cycle proven in step 1. The sumi route is untouched,
   so nothing to revert there.
6. **On success:** keep `voice-agent-sumi-prev` **stopped** through acceptance;
   retiring/removing it is a **separate authorization**, not part of this deploy.
7. Only after acceptance: SIP bring-up, then Eric's call.

**Receipts:** non-secret only — image ID / tag / digest, container status, labels.
Never the container env.

## Historical LiteLLM secret authority (superseded for production)

**Current state, 2026-07-26:** Sumi calls `http://sumi-local-llm:8080/v1`
directly and passes the explicit non-secret placeholder
`SUMI_LLM_API_KEY=local-no-auth`. No LiteLLM alias or 1Password item participates
in that path. The proposal below remains as reviewed history for a deliberate
future LiteLLM deployment; it is not a prerequisite or repair procedure for the
current worker.

The Slice-6 launch sourced the scoped LLM key from a `/tmp` captured file. It is
replaced with a **1Password-backed, least-privilege, op-run-injected** authority.
**None of the steps below have been executed** — no key minted, no item created, no
secret read or transferred. This section is the plan; the diff above is the artifact.

**Ownership / least privilege.** The key is Sumi's — a LiteLLM **virtual key allowed
ONLY the `sumi` model** (no master key, no broad/cloud fallback). Stored as 1Password
item `sumi-voice-litellm-api-key` (Harem World, `credential` field), referenced in the
**dedicated Sumi-only** `config/sumi-llm-key.env.tpl` (never the shared `livekit.env.tpl`,
which no longer carries the Sumi ref — E1), injected at launch via `op run` — kept out of
the repo, `/tmp`, argv, and shell history (see the exposure boundary below).

**Secure mint → 1Password transfer plan — TRANSACTION INVERSION (F3).** The failure
mode a naive `mint | op item create` invites is F3: if create's response is lost after
it commits, cleanup has no id and must fall back to a title lookup — concurrency-unsafe,
and it can delete the wrong item. The inversion removes that: the **immutable item id is
known before the secret exists**, so every mutation and every rollback targets an exact
id. The MASTER key never leaves the container; the child key crosses only stdout PIPEs
(mint → jq → `op item edit <id>` stdin) — never a terminal, log, file, argv, or history.
1. **Preflight — 3-state, fail-closed (F1/F2).** Both objects return `present | absent |
   unknown`; proceed ONLY when **both are CONFIRMED ABSENT**. The two absence proofs are
   now positive, not error-string parsing:
   - **1P item (F2):** absence is proven by a **successful `op item list --format json`**
     whose result lacks the exact title. ANY command/JSON failure → `unknown`. This closes
     the F2 hole where a network error like *"could not resolve host … no such host"*
     matched the substring "not found" and was misread as absent.
   - **LiteLLM alias (F1/C1):** absence is proven by a **successful `GET /key/list?key_alias=`
     exact-match enumeration** (verified in litellm source) whose `total_count == 0`;
     `total_count >= 1` is `present`. ANY non-2xx — including a proxy/wrong-path/unsupported
     **generic 404** — a missing `total_count`, a parse error, or a docker-exec failure is
     `unknown`. A bare 404 can no longer masquerade as "this alias is absent" (the earlier
     "any 404 → absent" was C1's fail-open).
   `present`/`unknown` both abort; a live object can no longer be mis-read as absent and
   clobbered.
2. **Create a NON-SECRET placeholder stamped with a UNIQUE per-run correlation (F3).**
   `op item create --vault "Harem World" --format json -` (positional `-`; `--template -`
   is INVALID, verified; category `API_CREDENTIAL`) with a `credential` field set to
   `PENDING-PROVISION` **and** a `provision_run` field set to a unique nonce generated
   before any mutation. Capture `.id` into `ITEM_ID`. If create returns **no id** (it may
   have committed but lost its response), we do NOT assume absent — the trap reconciles by
   the correlation (below). No secret exists yet.
3. **Mint IN-CONTAINER via an in-process HTTP client (G2).** The master key stays in the
   container env — never expanded into host/container argv. Fail-closed, nothing orphaned:
   **non-2xx** / **2xx-but-missing/malformed key** / **timeout** (possibly post-commit) →
   self-revoke by alias, exit non-zero; **else** `sys.stdout.write(key)` — the only emission.
4. **Update THAT EXACT id via stdin template; jq guards exactly-one-credential (F5).**
   `… | jq -Rs --argjson tpl "$json"` **errors unless the placeholder has exactly one
   `credential` field** (so a mis-shaped item can't leave the placeholder in place), patches
   that field to the minted key, and pipes to `op item edit "$ITEM_ID"` — op reads the JSON
   template on **stdin** (`cat tpl | op item edit <id>`, verified via `--help`), so the key
   never enters argv.
5. **Verified, correlation-anchored cleanup on ANY non-success (F3/G5).** A single EXIT trap
   revokes the alias, then finds **every item bearing OUR correlation** (via successful
   enumeration — never a bare title lookup), deletes them, and **PROVES** alias-absent AND
   no-correlated-item-remains. If either cannot be proven — enumeration fails, or an item
   lingers — it exits a distinct `CLEANUP_INCOMPLETE` (rc 4) with its own receipt. This
   genuinely covers commit-plus-response-loss on **create** (empty `ITEM_ID` → the orphan is
   still found by correlation), mint, and edit. (v4 falsely claimed create-loss was covered;
   it deleted only a known `ITEM_ID` and logged "verified" when the id was empty.)
6. **Stored-secret identity — BELONGS-TO, not just scope (F4/F5/B3).** `/key/info` is
   admin-only, so a virtual key cannot self-introspect there — verified in source. Identity
   is proven two ways: **verify B** uses the master key (admin) to assert the alias's
   `key_alias` **and** `models == [sumi]`, and captures the alias's stored **token hash**
   (`hash_token = sha256 hexdigest`, verified in litellm source). **verify C** authenticates
   the **actually-stored** key (resolved by `op run`, never printed) and asserts BOTH: (a) its
   `sha256` equals the alias's stored token hash — so the stored key *is the key behind alias
   v2*, not merely *some* sumi-scoped key (B3) — and (b) `/v1/models` is exactly `[sumi]`.
   Only sha256 **hashes** cross a process boundary; the raw key never does. A surviving
   `PENDING-PROVISION`, a wrong/broad key, or a different sumi-scoped key all fail.
7. **Exclusive lock + non-overridable correlation AND lock path (B4/C2/C3/G7).** An atomic
   `mkdir` lock at a **fixed canonical path** is acquired **before preflight**; a second
   concurrent run aborts (rc 5) without mutating anything. Both the per-run correlation and
   the lock path are internal and **not** externally overridable except under an explicit
   `PROVISION_TEST_MODE=1` — in production `PROVISION_LOCKDIR` is ignored, so two runs cannot
   pick different lock dirs and both proceed (C2), and an attacker-supplied correlation can't
   force a cross-delete. Lock release is **verified**: if `rmdir` leaves the dir behind, a
   success surfaces a distinct `LOCK-RELEASE-FAILED` (rc 6) that KEEPS the valid credentials
   rather than falsely reporting a clean run (C3). Every state read in cleanup captures its rc
   explicitly (`… && rc=0 || rc=$?`), so a docker/op failure mid-cleanup surfaces as
   `CLEANUP_INCOMPLETE` (rc 4) instead of a silent `set -e` early exit (B1/B2).

**Non-secret verification plan (rc / metadata only — never prints the value).**
1. Item exists: `op item get "sumi-voice-litellm-api-key" --vault "Harem World" >/dev/null; echo $?` → 0.
2. Reference resolves: `op read "op://Harem World/sumi-voice-litellm-api-key/credential" >/dev/null; echo $?` → 0.
3. Key scope + token hash (admin `/key/info` by alias): assert `key_alias=sumi-voice-worker-v2`
   and `models=["sumi"]`, and capture the stored `token` (sha256 hash) — never the raw value.
4. Stored-secret identity (NOT a nonempty smoke) — run on the **Sumi-only** injection surface
   (`config/sumi-llm-key.env.tpl`), never the 8-ref `livekit.env.tpl` (D1). verify-C first
   fails closed if **any** non-Sumi op-backed secret is present in its env (least-privilege
   sentinel), then requires the stored key to (a) `sha256` to the alias's stored token hash —
   proving it *belongs to* alias v2, not merely that it is non-empty or some sumi-scoped key —
   and (b) return exactly `["sumi"]` from `/v1/models`. Only hashes cross; the value is never
   printed. A surviving `PENDING-PROVISION`, a broad injection surface, or a different
   sumi-scoped key all fail.

**Implementation + discriminatory harness (single receipt).** Implemented as the
single-purpose, fail-closed helper `scripts/provision-sumi-llm-key.sh` (exclusive lock →
enumeration preflight → transaction inversion → mint → edit-by-id → hash-correlated verify →
correlation-anchored verified cleanup).

`scripts/test-provision-sumi-llm-key.sh` stands up a **local fake LiteLLM HTTP server**
(`/key/generate`, `/key/delete`, `/key/info` with a `token` hash, `/key/list` exact-match
enumeration, and `/v1/models` bearer auth), a **fail-on-demand fake `docker`**, and a
**stateful fake `op`** whose items carry id/title/correlation/**stored credential** and whose
`op run` injects the actually-stored value — so verify-C authenticates what is really stored.
The fake `op run` now **parses the `--env-file` template** and injects one fake value per
op:// ref it names — so a broad template is *visible* to the least-privilege sentinel (D1),
not silently masked. The fake `docker exec … python3 -` runs the helper's **real embedded
Python**, so no receipt is fabricated. **47 scenarios, every one a hard assertion, suite
exits 0, `bash -n` + `shellcheck` clean, no real LiteLLM/1Password touched:**
- **fail-closed preflight** — item-present, item-list-fails/host-not-found → UNKNOWN (F2),
  alias-present, alias-401 → UNKNOWN (F1), **docker-exec-failure → UNKNOWN** (B1), and a
  **generic route 404 on the alias endpoint → UNKNOWN** (C1) all abort (rc 3) with no create;
- **exclusive lock** — a pre-held lock aborts rc 5 with no mutation (B4); a **production-mode
  `PROVISION_LOCKDIR` override is ignored** (canonical path used, decoy untouched, C2);
- **correlation-anchored cleanup** — create-fail, mint-non-2xx/malformed/commit-drop, the F3
  **CREATE commit-drop with empty `ITEM_ID`** (orphan still found by correlation), edit-fail,
  and every verify failure roll back with the 1P state **proven empty**; a docker or
  enumeration failure *during* cleanup surfaces `CLEANUP_INCOMPLETE` rc 4 (B2/G5);
- **identity** — verify-B alias/models mismatch (F4), the **stale-placeholder** control (F5),
  and the **impostor** control (right scope, wrong hash → verify-C fires, B3) all roll back;
  success proves the server-minted key reached `op item edit` stdin, replaced the placeholder,
  and hash-correlates to alias v2 while scoped exactly `[sumi]`;
- **lock-release integrity (C3)** — a success whose lock cannot be removed surfaces
  `LOCK-RELEASE-FAILED` rc 6 while KEEPING the valid credentials (no rollback);
- **least privilege (D1)** — the broad shared template is rejected at **pre-mutation template
  validation** (E2, below); its runtime sentinel is separately proven by polluting a *valid*
  template's env with an extra forbidden secret → verify-C fires → rollback (belt AND braces);
- **malformed-200 shape (D2)** — `total_count=false` (bool-is-int), `-1`, missing `keys`, and
  `total_count=0` with non-empty `keys` all → UNKNOWN → abort, no create;
- **outcome truthfulness (D3)** — a preflight abort *after* acquiring the lock, with a forced
  release failure, exits **rc 7** with a "no credentials were created" receipt — never the
  provisioned-success rc 6;
- **shared-template removal (E1)** — the harness asserts `config/livekit.env.tpl` carries NO
  Sumi ref and the canonical SLICE-6 launch op-runs the Sumi-only template, not the shared one;
- **exact one-reference contract (E2)** — the Sumi-only template is validated *before any
  mutation*: an unknown **future** op-backed ref (`FOO_FUTURE=op://…`), a duplicate, a wrong
  URI, or a missing Sumi ref each abort **rc 8** with no mutation; the exact production template
  passes and proceeds. This catches names no blocklist knows;
- **negative controls** — the leak detector must fire on a planted key; UNKNOWN must never
  read as absent.

Building the suite has repeatedly caught real helper bugs (the two `set -e` masking sites in
B2/verify-C; this round the same class would have hidden C3's rc 6) — which is the point of a
harness that fails when a guarantee breaks. The count is taken from the run every time (an early "18" claim proved to be 17): v11 is **47** concrete scenarios.

**Real exposure boundary (honest).** `op run` by-name keeps the value out of the repo,
`/tmp`, argv, shell history, and any receipt. It does NOT make the value
un-inspectable: Docker persists the resolved env in the container's metadata, so a
privileged `docker inspect` can reveal it — inherent to running the worker, not a leak
this plan removes. The operative rule is therefore: **no repo/tmp plaintext, no
argv/history/receipts, and we never print or `docker inspect` the container env.**

**Old key + temp file are retained** as part of the preserved-old-container rollback
until the new worker is accepted; revocation/deletion is a later, separate step.

## Boundaries honored

No inference, no live Momo/LiteLLM mutation, no SIP activation, zero Tama/Shiori/
shared-Hermes config work. Unrelated voicebook-stream dirt preserved.
