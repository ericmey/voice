# Slice 1 — Stable managed `voicebook-stream` service — LANDED ✅

Final signoff: Yua second-read, 2026-07-23. The temporary externally-watched
qualification container is retired; `voicebook-stream` is a managed Compose
service with real ownership, rollback, and restart-proof paths — all proven on
mizuki before production mutation.

## Final topology

- **Service:** `voicebook-stream` (streaming TTS: `/speak/stream` raw s16le PCM
  24kHz mono, `/speak` WAV, `/healthz`). Server binds its port only AFTER model
  load + CUDA-graph warmup, so pre-ready probes get connection-refused (tolerated
  in `start_period`).
- **Host:** mizuki (RTX 5060 Ti 16GB), alongside `parakeet-ctl` and `litellm`.
- **Manager:** Docker Compose (`restart: unless-stopped` + stdlib healthcheck),
  NOT a watcher. The qual watcher is gone.
- **Networks:** attached to `voice_default` (external) so the Sumi agent reaches
  it by service DNS.

## Immutable image

- `voicebook-stream@sha256:3b28aa8102d69b3214687a7e732dcdeca35b8a11ab0d34187e1dad3f9b4472f7`
  (cu128 F1 — the observability-fixed build), `pull_policy: never`,
  `HF_HUB_OFFLINE=1`, model loaded from a pinned snapshot path (offline).
- Qualified FINAL COMPONENT GO; the F1 StreamingResponse outcome-classification
  fix (disconnect vs completion) is runtime-verified.

## Canonical project / worktree authority

- **Project name pinned** in the artifact: compose top-level `name: voicebook-stream`
  → lifecycle authority is cwd-independent; `down` targets ONLY this service
  (network + volume are external), never the broader `voice` project.
- **Durable home:** a dedicated **detached git worktree** at
  `/home/ericmey/voicebook-stream-deploy`, anchored at reviewed commit
  `87f1f5e39a495d7853b644df1001fa53837b97e3` — created from `/home/ericmey/voice`
  WITHOUT disturbing its `sumi-local-voice@341189d` checkout. Future updates are
  explicit worktree commit changes + hash/readback review.
- The ownership runbook pins `EXPECT_CANON_DIR=/home/ericmey/voicebook-stream-deploy`
  as a **literal, non-environment-derived** constant (a caller cannot redefine
  lifecycle authority via the environment).

## Port / DNS contract

- **Sumi's path (the real one):** `http://voicebook-stream:5060` via `voice_default`
  service DNS — proven reachable from the network.
- **Host ops/health ONLY:** `127.0.0.1:5056` → container `5060` (loopback). Sumi
  never uses host loopback. `livekit-sip` owns host `5060`, hence the `5056` map.

## Rollback tiers (fail-closed, no-two-model)

The migration and ownership runbooks never run their recovery paths for the first
time in production. Recovery proves **one complete tier** or reports failure:

- **A — canonical stable:** up rc=0, exact digest, running+healthy, project ==
  `voicebook-stream`, host 5056 + service DNS, Parakeet ready+live 200, VRAM > 800,
  tts stopped, qual stopped.
- **B — old-project stable:** same, but project == captured OLD_PROJECT.
- **C — qual fallback:** stable **definitively** absent or non-running (tri-state,
  never inferred from a failed command), qual exact digest running+healthy on 5060,
  Parakeet ready+live 200, VRAM > 800, tts stopped.
- Anything else → `ROLLBACK_FAILED`. Qual is NEVER started beside a running/unknown
  stable.

## Qualification / verification receipts

- **Mock fault-injection self-tests (zero container mutation):** main migration
  25/25, ownership 26/26 (16 scenarios + 10 meta-red-proofs); compose structured
  test + 6 red-proofs (host exposure, writable mount, injected env, injected
  secret, absent project name, wrong project name).
- **Supervised real rollback drill** (`MODE=drill`): forced failure before render
  → exit 1 + `ROLLBACK_OK`; proven recovery to the accepted qual on 5060 with a
  fresh sampling watcher and zero migration watchers.
- **Clean migration** (`MODE=clean`): exit 0 + `SUCCESS`; render 107564 B; VRAM
  free 5979 MiB; force-recreate restart-proof.
- **Ownership migration** (Authorization C): exit 0 + `SUCCESS`; independent final
  readback matched every item — canonical ps exactly `voicebook-stream`, old-project
  empty, labels correct, exact digest running+healthy, restart=unless-stopped,
  `voice_default` + DNS, render 200/103724 B, Parakeet 200/200, VRAM 6079 MiB, qual
  + tts stopped intact, zero watchers, worktree + base checkout unchanged.

## Lifecycle-ownership closure

The clean migration originally left the container labelled
`com.docker.compose.project=vbs-drill-a6a9c4e` (from the drill staging dir). A
staging dir cannot be the durable lifecycle owner (a different-cwd `compose` would
derive another project name and collide on `container_name`). The ownership
migration re-owned the running container to the canonical project from the durable
worktree, in place, fail-closed, self-rolling-back — eliminating the seam.

The old staging dir `/home/ericmey/vbs-drill-a6a9c4e` is left in place, explicitly
**superseded / non-authoritative**; retire it after this record is committed and
its contents are proven fully superseded.

## Artifacts (repo)

- `docker-compose.stream.yaml` (sha256 `cb3dc234…`)
- `scripts/migrate-stream-slice1.sh` + `scripts/selftest-migrate-slice1.sh`
- `scripts/test-stream-compose.sh` + `scripts/assert_stream_compose.py`
- `scripts/migrate-project-ownership.sh` (`1456e428…`) +
  `scripts/selftest-project-ownership.sh` (`73d4e11e…`)

Reviewed commit: `87f1f5e`.
