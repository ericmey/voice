# The phone leg — what the caller actually hears

**Measurements only. No cause is claimed for any artifact anyone has heard.**

This document covers one thing: the audio path **after** synthesis, and what can
and cannot be true about it. It does not cover the coalescing/split policy — that
is traced in `agents/sumi/src/voicebook_tts.py` and its regression test.

---

## The constraint that bounds everything else

From `livekit-sip` `using codecs` logs, **six inbound calls on 2026-07-25, no
exceptions**:

```
"audio-codec": "PCMU/8000"      "jitterBuf": false
```

**G.711 μ-law, 8 kHz.** Passband roughly **300–3400 Hz**, 8-bit companded
(~13 bits effective). We synthesise at **24 kHz**.

> **Everything above ~4 kHz is discarded before the caller hears it.** Grading TTS
> quality on the 24 kHz file grades audio no caller has ever heard.
>
> This is a **proven constraint, not a diagnosis.** It does not say the codec
> causes any artifact. It says upstream "max quality" work has a ceiling on the
> phone leg that no synthesis setting can raise.

**The decision boundary that follows:** if PCMU is fixed, the right target is
**intelligibility inside 300–3400 Hz**, not source fidelity. Those are opposite
optimisations, and picking the wrong one wastes the effort.

Whether the Twilio trunk can offer Opus is an **external question**, not a
prerequisite. If it can, the ceiling moves and everything upstream becomes worth
tuning.

## Stack currency — checked, and it does not support the premise

| component | ours | latest | gap |
|---|---|---|---|
| **livekit/sip** | v1.8.0 | v1.8.0 | current |
| livekit-server | v1.13.4 | v1.13.4 | current |
| livekit-agents | 1.6.7 | 1.6.7 | current |
| pedalboard | 0.9.24 | 0.9.24 | current |

**The commits were read, not assumed.** The audio-touching changes in sip
v1.6.0→v1.8.0 are: RTP destination redirect on re-INVITE, RTP port draining, and
three DTMF changes. **No codec, resampling, or quality work.** The update was
completed on 2026-07-26 as hygiene; it is not claimed as the fix.

Post-cutover acceptance used the installed 1.6.7 NVIDIA client and one fixed
caller WAV replayed three times through Parakeet. All three runs produced
**WER 0.0**, first interim at **156–164 ms**, and final text **24–26 ms** after
audio ended. Server, SIP, Sumi, Parakeet, Qwen, and Voicebook all remained at
`Restarts=0`. The synthetic E2E does not traverse PSTN/SIP media, so the next
real phone call remains the acceptance for the phone leg itself.

## Branches already settled — do not re-run these

- **The mastering EQ is already Kokoro-matched.** `agents/sumi/src/agent.py`
  annotates it *"Eric's accepted Kokoro mastering curve (blind sample B,
  2026-07-24)."*
- **A chunk-boundary hypothesis was already tested and failed.** `CHUNK_SIZE`
  moved 4→12 with the note that the 4-step window's ~320 ms decoder joins carried
  *"the same intermittent stutters Eric heard on the phone even though generation
  stayed >2× realtime and RTP had zero gaps."*

## Three hypotheses, kept separate on purpose

They share a lane, not a cause. Merging them into one paragraph would smuggle in a
common mechanism by adjacency.

1. `jitterBuf: false` on the SIP media port
2. the mastering EQ's **3250 Hz PeakFilter**, which sits on the G.711 rolloff edge
3. **gain/limiter → μ-law companding interaction** — +1.558 dB into a −3 dBFS
   ceiling, then an 8-bit log quantiser

---

## The instruments

```
(a) raw 24 kHz TTS        scripts/capture-voicebook-stream.py
(b) after mastering       scripts/apply-sumi-mastering.py
(c) after the phone leg   scripts/phone-leg-transform.py
```

**(b) imports the SHIPPED `_TelephonyMasteringProcessor` and refuses to run if
that import fails, rather than substituting a stand-in curve.** A reimplementation
would be another instrument to verify; if the fixture and the agent ever diverged,
the A/B would be measuring the fixture.

**(c) is bit-exact against ffmpeg's `pcm_mulaw`** — 9 hand-checked vectors, all
65536 int16 encode inputs, and all 256 decode codes enumerated. See
`--selftest`. Its first version was an idealised log curve that passed the
bandpass tone tests and was *not* G.711; the disproof is one value — **G.711
encodes silence to `0xFF`.**

The phone transform isolates the **codec**, not the transport. It reproduces no
jitter, packet loss or reordering, RTP pacing, carrier-side transcoding beyond
the local μ-law stage, or re-INVITE handling. In particular, it cannot test the
live `jitterBuf: false` path. Treat its output as the codec's contribution to the
samples, not as a synthetic PSTN call; only a real phone call can qualify the
packet path.

### What (c) does NOT reproduce — read this before trusting any number below

It reproduces the **codec**. It reproduces **none of the transport**:

- **no jitter** — and `jitterBuf: false` is one of the three open hypotheses
- **no packet loss or reordering**
- **no RTP pacing or timing**
- **no carrier-side transcoding** beyond our own
- **no re-INVITE handling** — which `livekit/sip` v1.8.0 changed

> **This tool measures what μ-law does to the samples. It cannot measure what the
> network does to the packets.**
>
> Every number in this document is *the codec stage, isolated*. **A synthetic E2E
> cannot prove the phone leg and neither can this — only a real PSTN call can.**

## Results — one utterance, the production E2E reply

Fixed text, `text_sha256 f8f09f9b…`. Raw: **9.92 s, 31 chunks, wall 6.73 s**
(~1.5× realtime), `audio_sha256 3e55f6c5…`.

### Mastering bypass, both through the phone leg

```
                       raw          mastered
input peak            -6.33 dBFS   -3.00 dBFS   limiter ceiling, hit exactly
energy outside
  300-3400 Hz         11.10 %       7.08 %
codec SNR             37.40 dB     37.42 dB
codec peak error     -42.03 dBFS  -36.27 dBFS
clipped samples        0            0
```

- Mastering raises peak 3.33 dB onto the ceiling. **Expected; the limiter's job.**
- Out-of-band energy drops. **The EQ removes content the phone discards anyway.
  Whether that helps in-band is not measured by this number.**
- **Codec peak error worsens 5.8 dB while SNR stays flat.** A louder signal into a
  log quantiser gives larger absolute error at peaks. **Not claimed as audible.**

> **Which of these sounds better through a phone is not answerable from these
> numbers.** It goes to Eric's ear. Both files were rendered through the μ-law
> transform and handed over for a blind A/B — the measurements cannot substitute
> for the calibrated instrument.

### The 109/64 request seam

Production split the reply into two `sumi-v1` requests. Reproduced:

```
single request     9.92 s
two requests      10.72 s      +0.80 s  (+8.1 %)

seam step at join:  1 LSB          <- no click
seam silence:       70 ms          <- A trailing 0.070 s + B leading 0.000 s
```

**Only 70 ms of the 800 ms is at the seam.** The other ~730 ms is *inside* the
segments: identical words, paced differently when the text is split. **A
distributed prosody difference, not a defect at one point in time.**

### First-audio latency — the other half of the trade

```
single request  173 ch    first_chunk 0.239 s
seg-a           109 ch    first_chunk 0.238 s
seg-b            64 ch    first_chunk 0.231 s
```

**Splitting buys ~1 ms.** voicebook streams: it emits as soon as the first window
decodes, so **total text length does not gate time-to-first-audio.**

**These captures fed complete strings**, and production does not — it feeds the
worker **incrementally from the LLM**, so coalescing lets sentence one dispatch
before sentence two has arrived. **This measurement structurally cannot see that**,
which is where the real win would live.

### Measured on the live turn — the head start is real (Yua, same day)

Rather than simulate a token cadence, the qualified E2E turn's own timestamps
answer it:

```
Voicebook request-109 synthesis start   ~15:33:22.838   (outcome ts − duration)
HTTP streaming response logged           15:33:23.069
Qwen completion                          15:33:23.101

dispatch head start                      ~263 ms
first stream access log before LLM end   ~32 ms
```

**So the policy buys ~0.26 s of head start on this turn.** Real, and **not a
law** — one real turn, one utterance, 173 characters.

### The two halves together

Combining that with the duration measurement above, on this one turn:

```
BENEFIT   first audio ~263 ms earlier
COST      total audio +800 ms longer, plus distributed prosody change

net       the caller hears the first word sooner and the LAST word
          roughly 540 ms LATER
```

**That arithmetic is worth stating and is not by itself a verdict.** For a phone
agent, time-to-first-word and total-utterance-time are not interchangeable —
early audio holds the caller's turn, and a longer utterance delays the handback.
**Which matters more is a product judgement, not a measurement**, and neither of
us has made it.

**No tuning change proposed.** `min_token_len=80` may be exactly right; the trade
is now quantified on both sides rather than assumed on either.

## Artifacts

`mizuki:~/tts-ab/` — `raw/`, `mastered.wav`, `phoned-raw.wav`,
`phoned-mastered.wav`, `seg-a/`, `seg-b/`, `seam-joined.wav`,
`phoned-seam-joined.wav`, `hashes.txt`, plus per-capture `manifest.json` with
per-chunk arrival times. **Every hash independently verified by Yua.**

**No worker or service was mutated to produce any of this.**
