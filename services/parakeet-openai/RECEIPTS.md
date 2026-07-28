# Receipts — the two OpenAI shims

Built 2026-07-28 by Aoi, under the boundary Yua ACKed (CID `openai-shims`):

> voicebook `/v1/audio/speech` as the narrow in-process alias to the shared
> completed-response helper under the existing lease/readiness/limits/logging/
> error contracts, and the separate OpenAI multipart to Riva gRPC transcription
> shim. **Build and test only; do not deploy either until I review your
> receipts.**

Nothing is deployed. mizuki is untouched — no image built there, no container
recreated, no config changed. The live TTS service on 5056 was *called* twice as
a client (to generate a test clip), which is ordinary traffic, not a change.

---

## 1. voicebook-stream — `POST /v1/audio/speech`

### What was done

`/speak`'s body was extracted verbatim into `_completed_response(req, rid, kind,
start)`. `/speak` now calls it. `/v1/audio/speech` translates the OpenAI field
names and calls the same function. **No HTTP self-loop** — a self-request would
take a second lease and deadlock against the one-flight design.

`kind` is the only thing that differs (`"unary"` vs `"openai"`), so an operator
can still tell the two doors apart in correlation records while the code
underneath is literally the same.

### Translation

| OpenAI field | handling |
| --- | --- |
| `input` | → `text` (required, min_length 1) |
| `voice` | → `voice_id` (required, min_length 1) |
| `model` | accepted as compatibility metadata, logged, **never dispatched on** |
| `response_format` | `wav` only; anything else is a typed **400** |
| `speed` | `1.0` only; anything else is a typed **400** |

`response_format` and `speed` are refused rather than ignored. A client that
asked for mp3 at 1.5× and receives 1.0× wav has been handed wrong audio wearing
a 200.

Both refusals happen **before** the lease is taken, so a bad request cannot
occupy a GPU slot.

### Behavior-preservation proof

The existing suite is untouched and was run against both `app.py` at HEAD and
`app.py` with the refactor:

```
app.py at HEAD      (git stash)   → 49 passed
app.py with alias   (restored)    → 49 passed
```

Same 49 tests, same result. The extraction moved nothing.

### New coverage — 17 tests, `tests/test_openai_speech.py`

Translation: input/voice map through to the synthesizer; `model` accepted and
ignored for two different values; `response_format` defaults to wav;
`X-Request-ID` echoed.

Refusals: unsupported format → 400 **with `synth.calls == []` and lease
unlocked**; unsupported speed → same; missing input → 422; missing voice → 422;
empty input → 422.

Shared contracts reached *through the alias*, each asserting the lease is
released:

- 4000-char limit — 4000 passes, 4001 → 413 `"never truncated"`
- unknown voice → 404 naming the voice
- not-ready → 503
- **lease held → 429, and recovers to 200 once released** (one-flight is
  fleet-wide, not per-route — otherwise the OpenAI surface is a second door
  around the concurrency limit the GPU budget depends on)
- synthesis failure → 502 carrying the real reason
- empty audio → 502, never a silent 200

Plus two structural proofs: identical input through both doors returns
byte-identical audio and the same registry entry; and the two doors log under
distinct `kind` labels.

```
$ PYTHONPATH=src aoi-verify --expect "66 passed" -- python -m pytest tests -q
66 passed, 1 warning in 0.22s
VERIFIED: real exit 0, output present, 1 sentinel(s) matched.
```

### Not proven here

The alias has **not** run against a real GPU synthesizer — there is no GPU on
the command chair. Every test above uses a fake synthesizer. What is proven is
the seam: translation, refusals, and that the shared path is genuinely shared.
The synthesis behavior below the seam is unchanged code, and the 49-test
before/after equality is the argument that it stayed unchanged.

---

## 2. parakeet-openai — `POST /v1/audio/transcriptions`

New standalone service, `services/parakeet-openai/`. No model, no state, no GPU.

### Why a shim rather than the redeploy

Parakeet publishes its own `/v1/audio/transcriptions` and ours answers
`{"detail":"Model not found for language en"}` — the `mode=str` profile serves
streaming gRPC and that route needs an offline model, which is not loaded. The
`mode=all` profile would want ~6–7 GB against the 3.6 GB the streaming profile
uses, and GPU1 already carries llama-server. Eric's call was explicit: shim
tonight, redeploy as its own project.

### Design decisions worth reviewing

**Audio normalization uses ffmpeg, not `audioop`.** My first draft used
`audioop`; it is deprecated in 3.12 and **removed in 3.13**, which would have
put a fixed expiry date on a brand-new service. It also is not a hand-rolled
resampler — correct resampling is real signal processing and this is a seam.
There is a genuine fast path: a wav already at 16 kHz mono s16le goes through
untouched with no subprocess, and a test monkeypatches `find_ffmpeg` to `None`
to prove the fast path is real rather than silently falling through.

`ffmpeg` is resolved to an absolute path (`shutil.which` plus known install
locations) because a daemon does not inherit a login shell's PATH. Its absence
is a clear `AudioError`, and the Docker build fails without it.

**Failure can never present as success.** Undecodable → 400. Oversize → 413
`"never truncated"`. Backend down → 503 (and `/healthz` red). Recognition failed
→ 502 with the real reason. An empty transcript at **200 means genuine
silence** — it is reachable only after a successful recognition, because every
failure took an error branch first.

**Only `is_final` results are joined.** Interim streaming results are partial by
definition; accumulating them produces duplicated, truncated text that reads as
a plausible transcript and is not one.

**Readiness is measured against the backend**, not against the process being up,
and the Riva client is constructed lazily so a slow Parakeet start shows up as a
red `/healthz` with a reason rather than a crash-loop with a restart counter.

**Bind is `0.0.0.0`, deliberately.** nyla.mey.house, hana.mey.house and the
command chair all need this. A loopback bind makes the service look healthy on
mizuki and be invisible to every caller — the exact fault that cost us Nyla's
5pm brief this morning.

### Unit coverage — 18 tests

Happy path in all three response formats; `model` accepted and ignored;
resample-before-recognizer (a 48 kHz stereo upload must reach the backend as
32000 bytes = 1 s of 16 kHz mono, not 192000); undecodable → 400; empty → 400;
oversize → 413; bad format → 400; backend error → 502 not empty text; genuine
silence → 200 with `""`; `/healthz` red when the backend cannot be built;
transcribe → 503 in the same condition; fast path skips ffmpeg; missing ffmpeg
is a clear error.

```
$ PYTHONPATH=src aoi-verify --expect "18 passed" -- python -m pytest tests -q
18 passed, 1 warning in 0.31s
VERIFIED: real exit 0, output present, 1 sentinel(s) matched.
```

### Live end-to-end — real Riva, real audio

The unit tests inject a fake backend, so `asr.py` itself was unproven by them.
Closed that gap: our own TTS spoke a known sentence, and the shim transcribed it
through the actual gRPC path to `10.0.20.25:50051`.

```
$ voice say -v aoi-v1 "The quick brown fox jumps over the lazy dog near the river bank."
  aoi-v1: 3.52s -> stt-probe.wav      (24 kHz — so the resample path is exercised)

healthz -> 200 {'status':'ok','ready':True,'backend':'10.0.20.25:50051',
                'language':'en-US','max_upload_bytes':26214400}

json          -> 200  {'text': 'The quick brown fox jumps over the lazy dog near the riverbank.'}
verbose_json  -> 200  {'task':'transcribe','language':'en-US','duration':3.52,'text': ...}
text          -> 200  The quick brown fox jumps over the lazy dog near the riverbank.

letter-exact match (whitespace/punctuation-insensitive): True
SMOKE_RESULT: PASS
```

**One honest note on the metric.** The first run of this smoke reported FAIL at
11/13 word recall, because Parakeet renders "river bank" as the single word
"riverbank". The transcript was verbatim-correct; my *comparison* was wrong. I
changed the metric to letters-only and said so, rather than lowering the
threshold — the check was the defect, not the service. `aoi-verify` caught it by
rejecting on a missing sentinel, which is the gate doing precisely its job.

### Compatibility with the client Hermes actually uses

Hermes' `_transcribe_openai` (`tools/transcription_tools.py:1355`) calls
`openai.OpenAI(...).audio.transcriptions.create` with `response_format="json"`
for any model that is not `whisper-1`. Rather than assert "it's OpenAI-shaped so
the SDK will work," I ran the real SDK against a real uvicorn over a real socket:

```
real uvicorn on 127.0.0.1:54115, real socket, real OpenAI SDK 2.49.0

response_format="json"   -> 'The quick brown fox jumps over the lazy dog near the riverbank.'
response_format="verbose"-> '...' (duration 3.52s)
unsupported format       -> refused via SDK: BadRequestError

SDK_COMPAT: PASS
```

### Correction to something I said earlier

I previously reported that Hermes supports only `local` / `groq` / `openai` STT
providers. That is wrong. The real fallback chain at
`tools/transcription_tools.py:852` is:

```
local > groq > openai > mistral > xai > elevenlabs > deepinfra
```

It does not change the plan — `openai` with a custom `base_url` is still the
right seam — but the claim was inaccurate and I would rather correct it than let
it sit in a receipt.

### Intended wiring (NOT applied — for Yua's review)

`_resolve_openai_audio_client_config` (line 1857) takes the config branch when
`stt.openai.api_key` is non-empty, and then honors `stt.openai.base_url`:

```yaml
stt:
  enabled: true
  provider: openai
  openai:
    api_key: local-no-auth      # non-empty is what selects the config branch
    base_url: http://10.0.20.25:5057/v1
```

Current state of the four profiles, verified tonight:

| agent | host | STT today |
| --- | --- | --- |
| nyla | nyla.mey.house | (checked, see profile) |
| sumi | nyla.mey.house | **`elevenlabs`** — cloud, paid, external |
| tama | command chair | **no `stt:` block at all** |
| shiori | command chair | **no `stt:` block at all** |

So the shim would take Sumi off a paid cloud dependency and give Fire & Ice ears
they currently do not have.

---

## What I want reviewed hardest

1. **The lease sharing.** `test_lease_contract_is_shared` is the test I care
   most about being right — if the alias could take its own lease, the OpenAI
   surface becomes a second door around the one-flight limit.
2. **Whether refusing `speed` is correct**, or whether silently synthesizing at
   1.0 is the friendlier behavior for SDK clients that always send `speed=1.0`
   explicitly. I chose refusal-on-mismatch; `speed=1.0` passes, so the common
   case is unaffected. This is a judgment call, not a proof.
3. **`asr.py`'s `is_final` filtering.** It is the one place where a subtle
   mistake would produce plausible, wrong output rather than an error.
4. The 25 MB upload bound — chosen to match OpenAI's own limit, not measured
   against our memory ceiling.
