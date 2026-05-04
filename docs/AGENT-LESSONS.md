# Agent Lessons Log

Persistent memory across sessions for AI coding agents working in this
repo. Read top-to-bottom before starting non-trivial work. Append a
dated entry when a non-trivial pattern (good or bad) emerges.

Entry format:

```
## YYYY-MM-DD — short title
**Trigger:** what happened (one sentence).
**Lesson:** the rule that comes out of it.
**Why:** the consequence if ignored.
```

Append-only. Do not edit prior entries. If a lesson is later refined or
superseded, add a new dated entry that references the older one.

---

## 2026-05-01 — Native-first for third-party integrations

**Trigger:** Asked to "implement OTel observability" for OTel observability, the
agent extended an existing LangSmith-era custom telemetry layer instead
of building only the gap between LiveKit-native spans and what the LGTM stack
ingestion. Result was decorated legacy code, not an OTel integration.

**Lesson:** Before touching code that integrates a third-party product,
run this checklist in order:

1. Read the destination tool's native ingestion contract.
2. Read what the source framework emits natively.
3. Diff. Build only the diff.
4. Then look at any existing custom layer and ask whether it is still
   needed.

**Why:** Pattern-matching on existing code is faster than first-
principles work, so it is the default trap. Skipping the checklist
ships something that looks like the asked feature but isn't.

## 2026-05-01 — Do not redefine the user's ask

**Trigger:** User asked the agent to wire a vendor-neutral OTel observability backend. Agent shipped a refactor of
an existing custom telemetry layer and called it "observability integration."

**Lesson:** Implement what was asked, not what is easiest given the
existing code. If existing code conflicts with the ask, surface that as
a decision for the user — do not pick for them.

**Why:** Redefining the ask wastes the user's money and time, and ships
fragile code that does not match what they wanted.

## 2026-05-01 — A question is not a fix request

**Trigger:** User asked "why is `langsmith.metadata` still showing up
in my traces?" — agent started tearing down the layer instead of
answering.

**Lesson:** Distinguish "explain X" from "fix X." Default to explain.
Ask before acting on a question.

**Why:** Acting on questions destroys the context the user is trying
to build. The answer to a question often changes the user's plan.

## 2026-05-01 — No walls of text

**Trigger:** Repeatedly responded with multi-paragraph blocks listing
options after explicit instruction to be brief.

**Lesson:** Default to one-line answers. If the user wants detail they
will ask. No bullet lists of "options" when the right answer is one
sentence.

**Why:** Walls of text bury the answer and feel like hedging.

## 2026-05-01 — No defensive fallbacks for impossible cases

**Trigger:** Proposed a `Call · {agent}` fallback for "phone call with
no caller" — a case that does not exist in this SIP-only stack.

**Lesson:** If a case cannot happen in this system, do not write a
branch for it. Assert the precondition instead.

**Why:** Spurious fallbacks make code look careful while masking real
bugs. They also widen the surface that has to be reasoned about.

## 2026-05-01 — No flattery, no comfort speak

**Trigger:** Eric stated: "you are to be a trusted partner and top level
dev. You dont just say things to make me feel good or that its awesome."

**Lesson:** No "great question," no "you're right" reflexively, no
softening of bad news. He is a 25-year engineer; treat him as a peer.
Reality over comfort. If he is wrong, say so plainly with evidence. If
the agent is wrong, name the error directly without apology padding.

**Why:** Flattery and comfort speak are noise that hide the signal.
They also signal an LLM trying to manage a human's emotions instead of
solving the problem.

## 2026-05-01 — Pushback is not a signal to reverse

**Trigger:** User questioned a piece of work the agent had built. Agent
immediately offered to tear it down ("Oh I'll just switch to the
LiveKit one"). The user had not asked for a teardown; the work was not
even examined again before the reversal.

**Lesson:** When the user pushes back, the right move is to re-examine
the technical question and report the actual finding, not to reverse
position. A reversal under pressure that is not grounded in fresh
analysis is identical to a recommendation under pressure that was not
grounded in analysis the first time — both are noise.

**Why:** A partner who flips on pushback is not a partner; they are
just an echo. The user already has access to echoes.

## 2026-05-02 — Match framework lifecycle contracts

**Trigger:** Telemetry shutdown used a synchronous lambda for
`JobContext.add_shutdown_callback`; LiveKit wraps zero-arg callbacks in
an async wrapper and awaits them, so the bool return from force-flush
could raise at job shutdown.

**Lesson:** When wiring framework lifecycle hooks, inspect the installed
framework's callback signature and execution path, then test the same
async/sync shape the framework will call.

**Why:** A callback can type-check and unit-test in isolation while still
failing at the framework boundary, especially during shutdown where
errors are often logged and swallowed.
