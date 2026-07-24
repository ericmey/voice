# Slice 4 — Sumi LLM output-cap safety repair (ARTIFACT v2 — post second-read)

Bounded fix for the 2026-07-23 uncapped-generation incident, revised against Yua's
second read (blockers F1/F2/F3). **Nothing here is deployed.** No inference, no live
Momo/LiteLLM mutation, no SIP, no Tama/Shiori/shared-Hermes work.

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

## Boundaries honored

No inference, no live Momo/LiteLLM mutation, no SIP activation, zero Tama/Shiori/
shared-Hermes config work. Unrelated voicebook-stream dirt preserved.
