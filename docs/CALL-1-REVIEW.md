# First live call — post-call review + optimization punch-list

**Call:** inbound PSTN (+1 317…) → `phone-sumi`, **40.3 s**, 3 assistant turns, clean
`CLIENT_INITIATED` disconnect (SIP BYE, cause 200 "User Triggered"). Capped worker
`voice-agent:sumi-2f126f3`, scoped key `sumi-voice-worker-v2` (models `[sumi]`).

**Cross-verified determination** (Aoi + Yua, two independent reviews, same conclusion):
the audible **blips are TTS chunk-seam artifacts** — **NOT** connection loss and **NOT**
LLM stalls. **No LLM or network change is recommended from this call.**

Doc-only review. No runtime changes made or implied by this file.

## Evidence (the real 40.3 s call)

- **SIP / RTP transport — clean.** 2,006 packets; 0 failed / 0 ignored / 0 lost / 0
  dropped; 0 mux gaps / 0 late; 43 delayed packets totaling ~15.6 ms; mixer dropped 0
  frames, 0 write errors, 0 blocked mixes. (Mixer `restarts=25` but **0 samples/frames
  dropped** — restarts without loss, which *confirms* nothing was dropped in transport.)
  End-to-end media latency avg 299 µs / max 469 µs.
- **LLM — healthy/fast.** 3 turns, TTFT 0.36–0.61 s, Momo evaluations 0.45–0.93 s, 0
  errors, not cap-limited.
- **TTS — the seam source.** 373 spoken characters were split into **11 separate
  synthesis/flush operations** (several only 12–30 chars), each independently generated
  and flushed; every one returned `outcome=ok`.

## Finding 1 — TTS seam artifact (CONFIRMED leading cause of the blips)

Eleven independent synth/flush ops over clean RTP with zero dropped mixer frames means
the blips are **perceptual discontinuities at clip boundaries** (prosody resets / clicks),
not lost audio.

Work, in order — **least-invasive first:**
1. **Punctuation-aware coalescing + a minimum chunk size** (merge the tiny 12–30 char
   fragments into sentence/clause-level chunks). Try this first.
2. **Crossfade / de-click at seams — ONLY if larger chunks do not remove the seam.**

**Proof gate:** a captured-audio **A/B** (before/after) that demonstrably removes the
audible seam. Do not declare fixed without it.

## Finding 2 — Early assistant termination (cause UNRESOLVED — do NOT call it VAD yet)

One assistant line ended mid-clause at "…sit in the quiet for". This is a **separate**
issue from the seams and its cause is **not yet established:**
- Momo decoded only **33 tokens** and reported **`truncated=0`** (non-truncated) — so
  **not** the 64-token cap.
- LiveKit recorded **0 interruptions**, and the agent stopped ~86 ms **before** the
  caller's next speech — so **not** a recorded barge-in.
- Remaining candidates: an upstream **model EOS/stop** in an incomplete clause, or an
  **unobserved cancellation**. Current evidence does **not** establish VAD.

Work — **classify before tuning:**
1. **Instrument** the upstream `finish_reason` + any cancellation reason on **every**
   assistant turn (worker-side logging).
2. **Reproduce**, then **classify** the cause. **No tuning of anything until classified.**

## Optimization observations (answering "any areas for optimization?")

- **TTS request efficiency:** 11 synth requests for 373 chars is chatty; the punctuation
  coalescing in Finding 1 improves **both** the seams **and** request count/latency — one
  fix, two wins.
- **Mixer `restarts=25`** over 40 s (no drops) is likely per-chunk republish churn; it
  should fall out naturally once chunks are coalesced. Worth confirming post-fix.
- **No infra optimization indicated:** media latency is negligible (~0.3 ms e2e), LLM TTFT
  is good, transport is clean. Do not tune the network or LLM off this call.

## Hygiene (low priority — NOT call blockers)

- Deprecated `metrics_collected` hook warning → migrate to `session_usage_updated` /
  `ChatMessage.metrics`.
- IPv6 STUN "network is unreachable" warnings while IPv4 succeeded → harmless; optionally
  disable IPv6 STUN to quiet ICE gathering.

## Separate must-fix-before-reuse (tooling, not the call)

- `scripts/provision-sumi-llm-key.sh`: every `docker exec "$LLM_CTR" python3 - <<PY` omits
  **`-i`**, so real Docker never attaches stdin and the embedded Python does not run. The
  live combined transaction bypassed the helper with explicit `-i`. **Fix:** add `-i` to
  all five exec sites, **and** make the harness fake `docker` REJECT `exec … python3 -`
  without `-i` (model Docker's real stdin contract) so the suite fails against the current
  helper and only greens once fixed. Helper is not to be reused until this lands.

## Owed infra (before any GPU / second-card work)

- Mizuki NVIDIA driver mismatch: loaded kernel module **595.71.05** vs installed/NVML
  **595.84**. Clean **reboot** aligns it (running GPU processes are fine; only new GPU
  init / `nvidia-smi` fail). One controlled outage; pairs with the eventual card install.

## Next diagnostic call

Capture the **outbound call audio** and **timestamp any blip** heard, to match each one
directly against the 11 TTS boundaries and confirm the seam hypothesis end-to-end.
