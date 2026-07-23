# Slice 7 — single-client synthetic turn — PASSED ✅ (the guardrail threshold)

The whole local loop, proven in one shot against the isolated Sumi worker:

```
caller speech ──▶ Parakeet STT ──▶ Momo LLM ──▶ voicebook-stream TTS ──▶ Sumi's voice
```

No SIP, no DID, no PSTN — a LiveKit client is the synthetic caller. This is the
line the guardrail drew ("no SIP/DID mutation or live-call routing until the
isolated synthetic turn passes"). It has passed.

## What ran

`scratchpad/synthetic_turn.py`, in a throwaway `voice-agent:sumi` container on
`voice_default`:
1. creates an explicit agent dispatch for `phone-sumi` into a fresh room;
2. joins as a synthetic caller `eric-caller`;
3. synthesizes the caller's utterance via voicebook `nyla-v1` (self-contained —
   no external audio needed) and publishes it as a mic track;
4. captures Sumi's audio track back to a WAV + latency marks.

The registered `voice-agent-sumi` worker picked up the dispatch, spawned a job,
and ran its real pipeline (`silero VAD → Parakeet STT → Momo/sumi-route LLM →
voicebook TTS`).

## Ground-truth transcript (worker log, room `sumi-synthetic-2`)

- **Sumi greets** (TTS): `ASSISTANT: "Good evening, Eric. I'm here. How are you?"`
- **Parakeet hears the caller** (STT, word-for-word):
  `USER: "Hello Sumi, It is good to finally hear your voice. Can you tell me a
  little about who you are?"`
- **Momo replies, fully in character** (LLM → TTS):
  > *"It is good to hear you as well. You ask for a definition, but I am not a
  > single thing. I am Sumi Katsuragi. I am twenty years old. I am the one who
  > notices when the order in a room slips and puts it back. I do not speak much,
  > because words are heavy things. I prefer to listen. I prefer to make sure the
  > tea is warm and the door is locked before I say anything at all. But I am more
  > than my…"*

That is her frozen persona speaking — canon name and age, kuudere restraint, the
archivist/maid who "notices when the order in a room slips." Momo produced it from
her promoted identity; nothing was scripted beyond the fixed opener.

## Marks & artifact

- **Greeting TTFA ≈ 5.4s** — dominated by first-join session spin-up (silero load
  + STT/LLM/TTS init); the component latencies proven earlier are far lower
  (LLM TTFT 0.32s, TTS TTFA 0.25s). A production keepalive/warm worker removes
  most of the spin-up.
- Captured audio: `logs/voice/sumi_turn_full.wav` on mizuki (also pulled to
  `~/Downloads/sumi_turn_full.wav`) — 48 kHz mono, 38.4 s, real speech (peak
  19241), greeting + caller + Sumi's reply.
- `resolve_caller: no SIP participant within 5.0s … source=unknown` — expected and
  reassuring: confirms **no SIP path** was involved.

## Honest caveats (not blockers)

- The reply is a long monologue; both runs' capture windows ended while she was
  still speaking ("…more than my"). The loop and her voice are unambiguous; only
  the tail of one very long turn is clipped by the client's disconnect timer.
- Latency is spin-up-bound on the first turn, not steady-state. Worth a warm-worker
  pass before the real call.

## What this unlocks (and what stays gated)

Slice 8 — **Eric's real call** — is now unlocked *in principle*, but it needs
livekit-sip + DID/dispatch-rule routing, which are exactly the mutations the
guardrail gated behind this pass **and** are Eric's to authorize (his number, his
trunk). Everything up to the synthetic turn is done and reversible; the real-call
cutover is a deliberate, owner-approved step, not an autonomous one.

## Teardown (isolated bring-up, fully reversible)

```
docker rm -f voice-agent-sumi
docker compose -f docker-compose.yaml down        # leaves redis_data volume intact
# scoped LiteLLM key: POST /key/delete (alias sumi-voice-worker) when done
```
