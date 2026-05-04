# SDK Open TODOs

## Re-enable `schedule_callback` via a structured cron path

**Status:** Tool is disabled — `@function_tool` decorator removed from
`SessionsToolsMixin.schedule_callback` so the voice model can't see it.
The method body + guardrails + guardrail tests are intact so the logic
is preserved for the re-enable.

### Why it's disabled

The current cron payload is a natural-language prose block that asks a
spawned agent to interpret it and then call `voice_call` itself. Three
problems:

1. **LLM in the loop.** Cron fires → spawns agent → model reads prose
   → model calls `voice_call`. Every model hop can wander (refuse,
   paraphrase, skip the base64 decode, call the wrong tool).
2. **Prompt-injection surface on our own reason field** — we're
   base64-encoding the reason string and telling the model "don't
   interpret this as instructions," which is evidence we're already
   defending against this shape of attack.
3. **Fragile scheduling contract.** Extending with priority,
   retry-policy, quiet-hours-override, "call only if the user is free,"
   etc. means cramming more prose into the message.

### Preferred fix: OpenClaw CLI verb (Option A)

Add a new CLI command to the `openclaw` binary:

```
openclaw voice_call initiate \
  --to <e164> \
  --mode conversation \
  --reason-file <path> \
  [--from <e164>]
```

- `schedule_callback` writes the reason text to a JSON file under a
  per-callback queue dir owned by the openclaw CLI (location is a CLI
  implementation detail — we just invoke `openclaw voice_call initiate
  --reason-file <path>`).
- `cron add --exec "openclaw voice_call initiate ..."` runs the CLI
  directly when it fires. No agent spawn, no LLM, no base64 dance.
- The CLI verb talks to whatever telephony backend is current (today
  that's LiveKit SIP + the configured outbound trunk).

### Alternative: Structured cron payload (Option B)

Keep the cron → agent flow but change the payload from prose to JSON
the agent parses deterministically. Still has the LLM-in-loop problem
but removes the injection surface. Do this only if Option A is blocked
by CLI-ownership concerns.

### Work to do when re-enabling

1. Land `openclaw voice_call initiate` CLI verb (separate repo).
2. In `SessionsToolsMixin.schedule_callback`:
   - Write `{reason, caller, target, delay_s, scheduled_at}` to a queue
     dir instead of embedding prose.
   - Invoke `cron add --exec "openclaw voice_call initiate --reason-file …"`
     instead of `cron add --agent <name> --message <prose>`.
   - Restore the `@function_tool` decorator.
3. Smoke test end-to-end: schedule a callback during a live call,
   verify the cron fires, verify the dial actually happens.
4. Re-add `schedule_callback` to the prompts' user-language → tool
   example blocks (Nyla, Aoi, Party).
5. Update the `test_all_eight_tools_present` assertion in each
   concrete agent test back to the previous `_nine_` form.

### Related state that must NOT be deleted while disabled

- `constants.CALLBACK_MIN_DELAY_S`, `CALLBACK_MAX_DELAY_S`,
  `CALLBACK_SHORT_DELAY_S`, `CALLBACK_QUIET_START_HOUR`,
  `CALLBACK_QUIET_END_HOUR`, `ERIC_TZ`, `parse_delay_seconds`,
  `is_quiet_hour` — guardrail constants + helpers.
- `tests/test_callback_guardrails.py` — direct-call unit tests that
  validate guardrail behavior without going through the tool decorator.
