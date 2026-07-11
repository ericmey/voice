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
  stop agent-aoi agent-nyla agent-yua agent-sumi
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
- **Container healthchecks** on redis + livekit-server (`docker ps` shows
  health); the four agents gate their start on livekit-server being healthy.
- **Cronned `scripts/health-check.sh --quiet`** every 5 min on mizuki →
  `~/.local/state/voice/health-check.log` (writes only on failure). Covers
  the agent-registration liveness the container healthchecks can't (agents
  have no self-probe endpoint).
- `make health` on demand — the same check, full output.
- `scripts/tail-logs.sh` for live watching with `--grep` filtering.
- `docker compose logs -f` for the infrastructure tier.
- [OBSERVABILITY.md](OBSERVABILITY.md) for OTel tracing, logs, metrics,
  dashboard import, host collector, and audio recording links.

### Future (not yet wired)
- Route the cron's failure output to a Discord webhook (currently
  file-only — a human/`/morning` reads the log).
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

**Root cause — CORRECTED 2026-07-11. The earlier diagnosis was wrong.**

This page previously blamed the Gemini realtime session's close hanging after
`EndCallTool(delete_room=True)` "tearing the room down out from under it". That is
**ordering-impossible** against the pinned `livekit-agents==1.6.5`, and it pointed the next
reader at `delete_room`. The actual teardown order in the shipped wheel:

| step | budget |
| --- | --- |
| `session.aclose()` — closes the Gemini realtime WS | **60s** |
| `session_end_fnc` | 300s |
| send `ShuttingDown` — **the parent's 10s kill clock starts here** | — |
| `room.disconnect()` | — |
| shutdown callbacks, incl. `delete_room` **and the OTel flush** | **10s (parent)** |

A hanging realtime close therefore cannot cause this: it runs *before* the clock starts,
with its own 60s budget, and would log `AgentSession.aclose() timed out after 60.0s` — which
the symptom never showed. And `delete_room` runs *after* `room.disconnect()`, when the
session is already closed. **`delete_room=True` is the library default and is correct. Do
not "fix" it.**

**The real cause: a synchronous OTel flush on the event loop.**
`sdk/tracing.py::wire_otel_shutdown_flush` registered a shutdown callback that *looked*
async but called `force_flush_otel_tracing()` **synchronously** — a blocking network export
to the collector on shiori, on the event loop. That starves every sibling shutdown callback
LiveKit gathers concurrently **and the IPC read/ping tasks**, so the child can no longer
answer its parent. At `shutdown_process_timeout` the parent sends SIGUSR1 — and **exit `-10`
IS SIGUSR1**. The process was shot; it never crashed.

Two details made it deterministic rather than occasional: the flush ceiling (10 000 ms) was
numerically identical to the kill budget (10.0 s), and the timeout was applied **per
provider** (traces, logs, metrics) rather than as a total — so a 10 s request could block
for **30 s**.

**Fixed** (`sdk/tracing.py`): the flush now runs on an explicit **daemon** thread with a
**total** 3 s budget, bounded well under the kill deadline. `asyncio.to_thread` is
deliberately *not* used — its ThreadPoolExecutor threads are non-daemon and the interpreter
joins them at exit, so abandoning a wedged flush would not actually abandon it; the hang
would simply move to a line nobody is watching. Covered by
`sdk/tests/test_shutdown_flush_does_not_block.py`, whose load-bearing test asserts the event
loop keeps ticking while the flush is wedged (it goes red against the old code).

**Still to validate on a real call.** Proven in tests; not yet reproduced-then-absent on
live telephony. That validation remains a deploy-session item.
