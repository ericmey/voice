# Operations runbook

Reference for common operational tasks. Every verb here has a `make`
wrapper; the bare `scripts/` invocations are shown for completeness.

If you are adapting this repo to a different agent, memory, SIP, or
telemetry stack, start with
[BRING-YOUR-OWN-STACK.md](BRING-YOUR-OWN-STACK.md).

## Day-to-day

### Deploy new code to all agents

Run on the agent host (`mizuki.mey.house`):

```bash
# For SDK, persona, or prompt changes — cycle rebuilds the shared
# voice-agent:latest image and recreates the agent containers.
make cycle

# One agent: rebuild the image, then recreate just that service.
docker build -f Dockerfile.agent -t voice-agent:latest .
docker compose -f docker-compose.yaml -f docker-compose.agents.yaml up -d agent-nyla
```

`make cycle` recreates the four `voice-agent-<name>` containers from the
freshly-built image. Docker sends `SIGTERM` on container stop, which
LiveKit agents handle by draining active jobs before exiting.

### Deploy a fresh machine

Do this on the agent host (`mizuki.mey.house`):

```bash
git clone <repo-url> voice
cd voice
make bootstrap                  # installs deps, drops config templates

# Edit the files bootstrap drops in ./config/ and
# ./secrets/livekit-agents.env.

brew services stop redis        # compose ships redis; avoid port clash
make up                         # infra: docker compose up -d
make register-sip               # register trunk + dispatch rules
make deploy                     # build the image + bring up the four agents
make health                     # confirm everything is green
```

### Change a SIP dispatch rule

1. Edit `config/sip-dispatch-<agent>.json`.
2. `make register-sip` — the script is idempotent and will delete the
   stale rule + recreate from the new JSON.
3. `make health` — verify rule count is back to ≥3.
4. Test with a live call; `scripts/tail-logs.sh --grep "dispatch rule matched"`
   on the sip container via `docker compose logs -f livekit-sip` to
   watch the match.

### Rotate an API key

1. Edit `secrets/livekit-agents.env`.
2. `make cycle` — rebuilds the image and recreates the agent containers,
   which reload the env file. No other file needs to change.

### Debug a silently-failing call

```bash
# First: live sip logs (this is where "flood" / "no-rule" / "no-trunk" show up)
docker compose logs -f livekit-sip

# Then: agent logs (what the model saw)
scripts/tail-logs.sh --grep "tool=|Error"

# Then: health check
make health
```

## Breakage recovery

### Agent workers all disconnect

Symptom: `make health` shows all agents missing `registered worker`
lines, or the SIP container 486s every call with `reason: no-rule`.

```bash
# Recreate all agent containers
make cycle

# Still broken? Stop the agents, then bring them back up.
docker compose -f docker-compose.yaml -f docker-compose.agents.yaml \
  stop agent-aoi agent-nyla agent-yua agent-party
make deploy
```

### `reason: flood` in livekit-sip logs

This is **not** rate limiting — it's `DispatchNoRuleDrop` (no matching
dispatch rule for the call). See [DISPATCH-RULE-GOTCHAS.md](DISPATCH-RULE-GOTCHAS.md).
Fix:

```bash
lk sip dispatch list            # confirm rules exist
make register-sip               # re-register from config/
```

### Compose stack won't come up

```bash
docker compose down -v          # -v nukes the anonymous volumes too
docker compose up -d
docker compose logs             # see what's erroring
```

Common causes:
- **brew redis still on :6379** — `brew services stop redis` first.
- **livekit-server port 7880 in use** — check `lsof -i :7880`.
- **Docker Desktop host-networking disabled** — enable in Settings >
  Resources > Network. livekit-sip needs it.

### Need to roll back to a previous SDK commit

Subtree imports mean the monorepo has full history for every subproject.
To roll a single subproject's files back:

```bash
# Find the old commit
git log --oneline sdk/

# Restore at that commit
git checkout <old-sha> -- sdk/
make cycle                      # rebuild the image, recreate agents
```

For a full-repo rollback, `git revert` the offending commit on main.

## Monitoring (current + future)

### Current
- `make health` on demand — intentionally minimal, exits non-zero.
- `scripts/tail-logs.sh` for live watching with `--grep` filtering.
- `docker compose logs -f` for the infrastructure tier.
- [OBSERVABILITY.md](OBSERVABILITY.md) for OTel tracing, logs, metrics,
  dashboard import, host collector, and audio recording links.

### Future (not yet wired)
- Cronned `scripts/health-check.sh --json` → Discord webhook on failure.
- Prometheus exporter on the agent worker count + livekit-sip call stats.
- Drift detection cron that re-reads `config/*.json` and diffs.

See [ARCHITECTURE.md](ARCHITECTURE.md) "Hardening direction" for the
sequencing.

## Known issues

### Realtime job process hangs ~10s on hangup, then is force-killed (exit -10)

**Symptom.** After a native-audio call ends, the agent log shows
`deleting the room because the user ended the call`, then ~10s of silence,
then `process did not exit in time, killing process` /
`sending SIGUSR1 signal to process` / `process exited with non-zero exit
code -10`. Intermittent — seen on aoi (the long thinking-partner calls),
not deterministic.

**Impact — benign, but noisy.** Post-call memory is safe: it runs in a
*detached* subprocess (`start_new_session=True`) that survives the kill and
completes independently (verified: `postcall_memory: completed
status=captured` lands before the kill). The call is already over. The only
cost is `ERROR`-level log lines on otherwise-healthy calls, which put a
false-positive floor under any error-rate alerting — the reason this is worth
fixing before wiring alerts, not just cosmetic.

**Root cause.** livekit-agents' `WorkerOptions.shutdown_process_timeout`
defaults to 10.0s. The job process doesn't exit within it because the Gemini
realtime session's close hangs after `EndCallTool(delete_room=True)` tears the
room down out from under it. The SIGUSR1 is livekit-agents asking the stuck
process to dump stack traces before killing it.

**Fix — validate before shipping.** Candidate approaches: (a) an explicit
session/realtime close in a shutdown hook before room deletion, (b) revisit
`delete_room=True` timing, (c) a larger `shutdown_process_timeout` *only if*
the close actually completes given more time. All of these change call
teardown and must be proven on a **real test call**, not assumed — bumping the
timeout blindly just delays the kill and masks the signal. Tracked as a
deploy-session validation item.
