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

## 2026-05-22 — Ops scripts must bootstrap their credential environment

**Trigger:** After a macOS update reboot, `make health` and `make register-sip`
failed even though LiveKit was healthy because non-login shells missed
Homebrew's `lk` path and the LiveKit CLI fell back to rejected dev credentials.

**Lesson:** Any operator script that calls credentialed CLIs must load the same
1Password-backed env template the deployed service uses, or fail with an
explicit credential-bootstrap error. Do not rely on an interactive shell's PATH
or pre-exported secrets.

**Why:** Health checks that fail from a clean shell train operators to ignore
them, and registration scripts that silently use dev credentials can report
false routing failures after every reboot.

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

**Trigger:** The operator asked agents to act as trusted senior
engineering partners, not as reassurance generators.

**Lesson:** No "great question," no "you're right" reflexively, no
softening of bad news. Treat the operator as a peer. Reality over
comfort. If they are wrong, say so plainly with evidence. If the agent
is wrong, name the error directly without apology padding.

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

## 2026-05-04 — Migration defaults must move with docs

**Trigger:** The observability backend migrated from a local collector
to a remote OTLP stack, but `scripts/deploy-agents.sh` still defaulted
`VOICE_OTLP_ENDPOINT` to `http://localhost:4318/v1/traces` when the
secrets file omitted the variable.

**Lesson:** Backend migrations must update deploy-time fallbacks and
code defaults, not just examples and docs. Search for old endpoints,
old environment labels, and comments that omit required OTLP signal
paths like `/v1/traces`.

**Why:** A clean deploy can look correctly documented while launchd
quietly renders a stale endpoint, causing telemetry to disappear
without an application failure.

## 2026-05-04 — Verify model IDs against primary docs

**Trigger:** During a LiveKit agent setup review, a model string looked
invalid from a broad search result, but the model-specific Gemini docs
showed `gemini-3.1-flash-lite-preview` was valid.

**Lesson:** For provider model IDs, open the model-specific primary
docs (or inspect the installed provider's default) before changing code.
Search snippets are not enough.

**Why:** Model names move quickly and search results can surface stale
or partial tables. A well-intentioned "fix" can downgrade or break a
working agent.

## 2026-05-05 — LiveKit SIP identity can carry caller number

**Trigger:** A real inbound SIP call resolved `source=sip` but
`caller_from=None` because the SIP participant attributes omitted
`sip.from`, while the participant identity was still
`sip_+13179957066`.

**Lesson:** Treat SIP participant attributes as authoritative when
present, but fall back to the `sip_<E.164>` participant identity for
caller number enrichment.

**Why:** Missing caller numbers make traces, post-call review, memory,
and callback tooling harder to trust even when the call itself was
correctly routed.

## 2026-05-05 — Treat voice subprocess boundaries as security boundaries

**Trigger:** A live post-call hook failed because the deploy rendered a stale
path for an external CLI binary, and a review of voice tool subprocesses showed
the agent runtime trusted that path before launching it.

**Lesson:** Validate external executables at both deploy time and runtime:
absolute path, executable, expected basename, and not world-writable.
Keep subprocess command verbs allowlisted and arguments bounded.

**Why:** Voice tools are actuators. Even when `shell=False` prevents shell
injection, a bad binary path or unbounded argv payload can turn a normal
tool call into an unsafe process boundary.

*(The CLI this was written about is retired; the boundary rule is not. The
post-call memory extractor still spawns a subprocess.)*

## 2026-05-05 — Keep OTel resource labels semantically true

**Trigger:** Grafana/Loki showed `service.version=signoz-primary` and
then `grafana-stack` after an observability migration, even though
`service.version` should identify the running app build.

**Lesson:** Use `service.name` for the component, `service.version` for
the code/release/git SHA, and `deployment.environment` for the runtime
environment. Do not encode backend names or migration phases in resource
identity.

**Why:** Misusing resource labels makes debugging feel haunted: filters
look like services or dependencies that no longer exist.

## 2026-05-04 — Public examples must stay generic

**Trigger:** A public-readiness sweep found docs and examples that still
named private hosts, old split-repo paths, and operator-only migration
notes after the monorepo and telemetry changes landed.

**Lesson:** Public-facing docs, templates, and fallback defaults should
use generic local examples unless a value is intentionally part of the
project contract. Keep private deployment hostnames, channel IDs, and
operator runbook history in local secrets or private notes.

**Why:** Example values get copied into real deployments. Private labels
also make a public repo harder to evaluate and can expose internal
topology without adding useful context.

## 2026-05-05 — Voice agents should delegate, not reroute — **SUPERSEDED 2026-07-09**

**Trigger:** The phone agents had dedicated `academy_*` and session tools
that knew about Mizuki, Discord targets, and a local agent CLI.

**Lesson (as written):** Treat the voice agents as live conversation front
doors into the gateway, not separate implementations of its personas. Default
to a single fire-and-forget hook delegation path.

**Superseded:** The gateway was retired on 2026-07-09 and the delegation
surface was deleted outright. There is now no route off the phone. The
enduring half of this lesson is the negative: do not reimplement another
system's routing inside the voice stack. The positive half — "delegate
instead" — no longer has anywhere to delegate to. A prompt that promises a
delegation tool the runtime does not register makes the model fabricate,
which is why the prompts lost those sections too.

## 2026-05-05 — Cycle LiveKit workers with TERM, not force

**Trigger:** `make cycle` used `launchctl kickstart -k`, and Loki showed
LiveKit's process pool raising `Cannot close a process while it is still
running` during redeploy windows.

**Lesson:** LiveKit agents drain active jobs on `SIGTERM`/`SIGINT`.
Operations scripts should send TERM, wait for the old PID to exit, and
reserve forced `kickstart -k` for explicit timeout escape hatches. Do not
depend on launchd stop behavior alone; scripts should control the
TERM-and-wait sequence directly.

**Why:** Forced restarts can interrupt active phone calls and create
scary-but-avoidable ERROR logs. Letting LiveKit drain keeps redeploys
aligned with the worker lifecycle.

## 2026-05-05 — Four voice agents are the steady state

**Trigger:** PR review questioned whether health and deploy should preserve
pre-Yua subset workflows that intentionally leave one of the four phone agents
undeployed.

**Lesson:** Treat Nyla, Aoi, Yua, and Party as the supported steady-state
voice stack. It is still fine for deploy scripts to accept a subset for
targeted repair, but `make health` should fail when any steady-state agent or
its SIP rule is missing.

**Why:** Health checks are the operator's trust signal. If the intended system
has four phone agents, a missing Yua worker should be visible immediately
instead of hidden as a "partial deploy" success.

## Python audio packages clobber a verified cu128 torch (Blackwell, sm_120)

**2026-07-22.** Three separate packages did this in one session on mizuki:

| package | what it did |
|---|---|
| Orpheus-FastAPI | `Dockerfile.gpu` installs torch from the **cu124** index |
| `qwen-tts` | pulled **torchaudio cu130** against cu128 torch — hard import error |
| `chatterbox-tts` 0.1.7 | hard-pins **`torch==2.6.0`** (cu124, arch list ends `sm_90`) |

The failure is quiet. The container builds, the service starts, requests
succeed, and the GPU is never touched. The stack trace, when there is one,
points wherever CUDA was first used — `cudnn_rnn_flatten_weight`, `torch.min` —
which sends you debugging the wrong layer.

**Verifying cu128 BEFORE installing the package is not enough.** I did exactly
that with chatterbox: watched `sm_120` present, installed the package, and let
it silently undo the environment. Install order is load-bearing:

```bash
pip install <package>
pip install --force-reinstall torch torchaudio \
  --index-url https://download.pytorch.org/whl/cu128
```

`pip check` will then report a violated pin. Record that as **"works with
unsupported dependency versions,"** never as "supported."

**Assert it, don't trust it.** `services/voicebook-tts/Dockerfile` fails the
*build* if `sm_120` is missing from `torch.cuda.get_arch_list()`, and
`QwenBaseSynthesizer.__init__` fails at *construction* rather than mid-request.
Both exist so this defect cannot recur silently.

Re-check `get_arch_list()` and run a real CUDA op after **every** dependency
change — not once at environment creation. For a running service, confirm
residency with `nvidia-smi --query-compute-apps`, not logs.

## 2026-07-24 — Test doubles must model Docker stdin attachment

**Trigger:** A provisioning helper used `docker exec <container> python3 -`
without `-i`; its fake-Docker harness forwarded stdin unconditionally, so 47
scenarios passed while real Docker gave Python an empty script.

**Lesson:** A test double for `docker exec` must withhold stdin unless the
command includes `-i`. For embedded scripts, red-prove the unfixed command
against that behavior before accepting the repair.

**Why:** A harness that models the desired contract instead of the real one can
make non-execution look fully verified, especially at shell/process boundaries.

## 2026-07-24 — Non-streaming TTS needs an explicit chunk policy

**Trigger:** Sumi's first real call delivered every RTP sample but synthesized
373 characters as 11 independent clips, including 12--30 character fragments;
the resulting prosody resets were audible as blips.

**Lesson:** When LiveKit wraps a full-text TTS engine, construct the
`StreamAdapter` explicitly with a tested minimum and maximum phrase size. Do not
let the SDK's per-sentence default silently define the production audio seam.

**Why:** Transport can be lossless while independently generated clips still
sound discontinuous. Coalescing improves naturalness and reduces request and
mixer churn with one worker-side change.

## 2026-07-24 — A workspace member is not covered until the gates enumerate it

**Trigger:** Sumi was a valid uv workspace member, but `make test` still listed
only the older agents and Pyright had no Sumi execution environment. The public
gate reported success without running her tests and resolved `agent.py` imports
against another package.

**Lesson:** When adding an agent package, update both the test-loop enumeration
and the paired `src`/`tests` Pyright execution environments. Prove the package
name appears in the gate output.

**Why:** Workspace discovery installs a package; it does not automatically make
hand-enumerated verification loops or ambiguous same-named modules cover it.

## 2026-07-24 — Isolate same-named workspace modules during tests

**Trigger:** Once the root test gate began running service packages, the
`voicebook-tts` service tests imported Sumi's different top-level
`voicebook_tts.py` from the shared editable environment.

**Lesson:** Run each workspace member with its own absolute `src` directory at
the front of `PYTHONPATH`. A shared uv environment does not disambiguate two
packages that intentionally expose the same top-level module name.

**Why:** Test collection can execute the wrong package while every dependency
is technically installed, producing either false failures or—worse—tests of the
wrong implementation.

## 2026-07-24 — Real-time DSP must preserve state across audio frames

**Trigger:** Sumi's accepted Kokoro mastering curve was moved from an offline
render into LiveKit's frame-by-frame `tts_node`; resetting each filter for each
frame would have manufactured the same boundary discontinuities the mastering
pass was intended to remove.

**Lesson:** Instantiate one DSP chain per utterance and process consecutive
audio frames with filter state preserved. Keep a true bypass for A/B, assert
unchanged frame geometry, and contain overload without integer wraparound.

**Why:** An offline effect can sound correct while a stateless real-time port
clicks at every frame boundary. The streaming lifecycle is part of the audio
contract, not an implementation detail.

## 2026-07-24 — Model-loaded is not caller-ready

**Trigger:** The faster-whisper service loaded its weights in about two seconds
and reported the model resident, but the first real transcription could still
pay a separate CUDA/kernel initialization penalty. A one-shot warm sidecar also
would not rerun when Docker restarted only the main service.

**Lesson:** Put warmup in the service's own restart lifecycle and exercise the
actual inference endpoint before declaring health. Health must prove the exact
model is resident *after* that inference, not merely that HTTP answers or a
model manager lists weights.

**Why:** Readiness that stops at process or weight load quietly transfers cold-
start work to the first caller. For an interactive demo, that is a functional
failure even though every infrastructure check is green.

## 2026-07-24 — Prefer maintained semantics over a “realtime” label

**Trigger:** Speaches v0.9's realtime websocket accepted streaming audio but
buffered the utterance until VAD completion and then called its batch endpoint.
It also required an older session schema and exposed two release-specific
failure seams around loopback routing and response generation.

**Lesson:** Inspect what a provider surface actually does. When “realtime” is a
websocket wrapper around utterance-final batch recognition, use LiveKit's
maintained VAD plus `StreamAdapter` around the proven batch endpoint instead of
carrying a private protocol compatibility fork.

**Why:** The production contract is accurate endpoint-to-final transcription,
restart safety, and predictable latency—not the transport's name.

## 2026-07-24 — Bias only the vocabulary the real call proves weak

**Trigger:** Faster-whisper preserved `Mizuki`, `35%`, and `1030 Monday` in a
controlled phone discriminator but heard utterance-initial `Sumi` as `Subi`.
The model was fast and otherwise semantically correct, so a provider swap would
have discarded a qualified path to chase one measured proper-noun failure.

**Lesson:** Pass a small, explicit, environment-overridable domain prompt into
the existing transcription request. Assert the prompt at the provider boundary,
then red-prove that assertion by removing the handoff before accepting green.

**Why:** Domain bias can repair a short-name language-prior error without
changing latency, endpointing, or model residency. A test that has never failed
does not prove that the new value actually crosses the provider boundary.

## 2026-07-26 — Recovery can manufacture green while hiding the fall

**Trigger:** Riva finished TensorRT construction, failed to load its acoustic
model with a GPU OOM, and logged the failure—but its Python wrapper returned
normally. Docker recorded exit code 0, and `restart: unless-stopped` repeatedly
rebuilt the model. The service looked busy and recoverable while never serving.

**Lesson:** A recovery loop is not evidence of recovery. Preserve the failing
layer, inspect the outcome behind the wrapper, and require a real request under
the final co-resident load. Make fatal server-start failures exit nonzero when
the wrapper is ours; when it is not, monitor the served outcome rather than the
process exit code.

**Why:** Restart policy, health grace, and wrapper semantics can compose into a
flattering false signal. The more reliably the safety net repeats the failed
operation, the more progress it appears to make.

## 2026-07-26 — Health grace belongs to the current startup path

**Trigger:** A one-hour `start_period` was correct while every Parakeet recreate
performed a measured ~45-minute TensorRT build. Once engines became a durable
provisioned artifact, the same service reached ready/live in 24 seconds; keeping
the one-hour grace would only suppress real failures.

**Lesson:** Recalculate readiness grace whenever startup work moves across a
lifecycle boundary. Provisioning-time work must not continue defining the
serving-time failure window. Use observed serving startup plus explicit margin.

**Why:** A grace period can become a lie without changing a single number. Too
short marks expected work unhealthy; too long makes a broken production start
look expected.

## 2026-07-26 — Concurrency is observed over the request lifetime

**Trigger:** A load harness labelled runs from the requested worker count even
when it had fewer work items, then replaced that label with a computed upper
bound. Its first observed counter used a cyclic barrier, so a second partial
wave waited for the timeout and manufactured a 120-second p95.

**Lesson:** Report concurrency only from a counter incremented after a one-shot
first-wave release and held until each response stream is exhausted. Record the
observed peak in the receipt. Requested workers and available tasks are inputs,
not measurements. A broken first wave must also open the one-shot event so later
work cannot strand. After repairing an instrument, red-test the replacement and
ask what its new number measures; a corrective result gets no reduced scrutiny.

**Why:** A plausible capacity label can survive reviews and size production
hardware incorrectly. A barrier or counter bug can then make the corrective run
look authoritative while measuring the test fixture's own delay.
