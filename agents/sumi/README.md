# agent-sumi

Sumi Katsuragi's voice agent — the archivist / background maid process.

**Slice 2 (identity/package) — artifact only.** Forked from `agent-party` as the
compile/test scaffold. Sumi's identity, memory isolation, fail-loud persona, and
entrypoint token mapping are hers; the STT/LLM/TTS pipeline is still the inherited
CHAINED cloud scaffold and is **not run**. No container, worker registration,
cloud request, or Musubi write happens in this slice.

Her local pipeline is wired one component per slice:

- **Slice 3 — STT:** Whisper → Parakeet/Riva (streaming) via the official LiveKit
  NVIDIA/Riva plugin. (Proven 2026-07-23: `riva_streaming_asr_client` transcribes
  her voice word-for-word; the deployment is streaming-only, so Slice 3 is adapter
  wiring.)
- **Slice 4 — LLM:** Gemini → Momo (local readable route).
- **Slice 5 — TTS:** ElevenLabs (Nyla's id, scaffold) → the managed
  `voicebook-stream` service in Sumi's own accepted master voice
  (`canon/people/sumi/voicebook/sumi-voice-master.wav`).

## Identity

- `agent_name` = `sumi` → `registration_name` = `phone-sumi` (derived).
- memory: `sumi/voice` namespace, `sumi-voice` tag, dedicated
  `MUSUBI_V2_TOKEN_SUMI` bearer (entrypoint-mapped).
- persona: `prompts/system.md`, source-backed from promoted harem-ops canon,
  **fail-loud** (missing or empty prompt refuses to start — no generic fallback).

## Persona provenance

`prompts/system.md` and the `_GREETING` are assembled from harem-ops canon
(commit `0d2d81a`):

- primary: `canon/people/sumi/profile.md` (`ccb12d5e`),
  `canon/people/sumi/psychology.md` (`e56cee02`)
- secondary: `canon/people/sumi/role.md` (`a993fd4f`),
  `canon/people/sumi/relationships.md` (`fa6fff4a`)
- delivery notes: `canon/people/sumi/voicebook.md` (`ece4a69b`)

Excluded from the volunteered prompt: the half-sister-through-Katsuya fact
(secret canon — private internal knowledge only if Eric approves), the
relationships LEGACY block, and stale wiki operational-role text.

The literal persona text and greeting are gated on **Eric's PASS**.
