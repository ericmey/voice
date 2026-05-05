# Operations runbook

Reference for common operational tasks. Every verb here has a `make`
wrapper; the bare `scripts/` invocations are shown for completeness.

## Day-to-day

### Deploy new code to all agents

```bash
# For SDK changes — cycle picks up the new shared code.
make cycle

# For an agent-specific persona/prompt change — cycle also works.
make cycle                      # cycles all three
scripts/cycle-agents.sh nyla    # cycle one
```

`make cycle` is graceful by default: it sends `SIGTERM` to each launchd
job, waits for the old PID to exit, and lets LiveKit drain active jobs
before the replacement worker registers. The default wait is 1860s,
slightly longer than LiveKit's 30-minute worker drain timeout. Override
with `LIVEKIT_AGENT_DRAIN_WAIT_SECONDS=<seconds>` if needed.

For emergencies only, set `LIVEKIT_AGENT_FORCE_ON_TIMEOUT=true` to fall
back to `launchctl kickstart -k` after the drain wait expires. Forced
restart can interrupt an active call and may emit LiveKit process-pool
shutdown noise in Loki.

### Deploy a fresh machine

```bash
git clone <repo-url> openclaw-livekit
cd openclaw-livekit
make bootstrap                  # installs deps, drops config templates

# Edit the files bootstrap drops in ./config/ and
# ./secrets/livekit-agents.env.

brew services stop redis        # compose ships redis; avoid port clash
make up                         # docker compose up -d
make register-sip               # register trunk + dispatch rules
make deploy                     # render plists + install agents
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
2. `make deploy` — re-renders plists with the new value, temporarily
   disables launchd restart, sends `SIGTERM`, waits for LiveKit drain,
   then bootstraps the updated plist. No other file needs to change.

### Configure OpenClaw delegation

Voice agents delegate outside work through OpenClaw Gateway hooks, not
the `openclaw` CLI. Set these in `secrets/livekit-agents.env`:

```bash
OPENCLAW_HOOK_TOKEN=<dedicated Gateway hooks token>
OPENCLAW_GATEWAY_HTTP_URL=http://127.0.0.1:18789
OPENCLAW_HOOKS_PATH=/hooks
```

Keep `OPENCLAW_HOOK_TOKEN` distinct from `GATEWAY_AUTH_TOKEN`. On the
OpenClaw side, enable hooks and constrain `hooks.allowedAgentIds` to the
agents the phone stack may route to.

### Test tools without a phone call

Use the no-phone harness for routine regression checks:

```bash
make voice-harness
uv run python sdk/scripts/voice_tool_harness.py --agent aoi --case ops-check
```

Mock mode is default and sends nothing to OpenClaw. Add `--live-hooks`
only for an intentional Gateway acceptance smoke. See
[VOICE-TOOL-HARNESS.md](VOICE-TOOL-HARNESS.md).

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

Symptom: `make health` shows all three agents missing `registered worker`
lines, or the SIP container 486s every call with `reason: no-rule`.

```bash
# Cycle all three
make cycle

# Still broken? Full teardown and redeploy.
make teardown
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
make cycle                      # rebuild venvs if needed, restart agents
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
