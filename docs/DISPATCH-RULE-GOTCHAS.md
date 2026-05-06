# SIP dispatch rule gotchas

Traps I fell into so you don't have to.

## The `numbers` vs `inbound_numbers` trap

On `SIPDispatchRuleInfo` (the modern, wrapped form of
`CreateSIPDispatchRuleRequest.dispatch_rule`):

| Field | Filters on | Source proto |
|-------|-----------|--------------|
| `numbers`         | **dialed DID** — `req.CalledNumber` (the TO leg) | `SIPDispatchRuleInfo.numbers` (field 13) |
| `inbound_numbers` | **caller's own number** — `req.CallingNumber` (the FROM leg) | `SIPDispatchRuleInfo.inbound_numbers` (field 7) |

**In the DEPRECATED flat form** of the same Create request (no
`dispatch_rule` wrapper, fields 1–9 directly at the top level),
`inbound_numbers` had *inverted* meaning — it was the TO filter. The
migration to the wrapped form renamed that field's meaning without
renaming the field. Confusing.

### Symptom

If you put dialed DIDs in `dispatch_rule.inbound_numbers`, **no rule ever
matches any real call**. livekit-sip closes the call with
`status: 486, reason: flood`.

### Why "flood"?

`reason: flood` in the livekit-sip log is emitted from **two** code paths
([`pkg/sip/inbound.go`](https://github.com/livekit/sip/blob/main/pkg/sip/inbound.go)):

1. `AuthDrop` — trunk auth rejected the call (misleadingly labeled "flood"
   in source).
2. `DispatchNoRuleDrop` — trunk accepted the call, but no dispatch rule
   matched. Also labeled "flood".

The log message doesn't distinguish. If you see `flood` with a `sipTrunk`
field set and no `sipRule` field in the close event, it's path 2 — no
rule matched.

### Correct wrapped-form rule

```json
{
  "dispatch_rule": {
    "name": "twilio-to-phone-nyla",

    "numbers": [
      "+13176534945"
    ],

    "rule": {
      "dispatchRuleIndividual": {
        "roomPrefix": "phone"
      }
    },

    "room_config": {
      "agents": [
        {
          "agentName": "phone-nyla",
          "metadata": "{\"route\":\"default\",\"source\":\"sip\"}"
        }
      ]
    }
  }
}
```

### Practical rule of thumb

- **Want to route by DID** (almost always): use `numbers`.
- **Want to allowlist callers at the rule layer**: use `inbound_numbers`.
- **Want to allowlist callers at the trunk layer** (usually preferred):
  use `allowed_numbers` in the inbound trunk JSON.

## Other traps worth knowing

### Dispatch rule without a trunk

If you don't pass `--trunks <id>` when creating a dispatch rule, livekit
registers it as a **wildcard** rule (no trunk filter). Whether that's
what you want depends — for this setup it is not; we always want a
specific trunk binding. The `scripts/register-sip-routing.sh` script
looks up the active trunk and passes it explicitly for every rule.

### Multiple rules matching the same call

If two rules would match (same DID in two `numbers` lists, for example),
livekit picks by priority (`hasHigherPriority` in the matcher). Priority
is implicit — numerically lower ID wins, or the newer one if IDs are
equal. Safer to not have overlapping rules.

### `lk sip dispatch delete` flag

Takes the rule ID as a **positional arg**, not `--id`:

```bash
lk sip dispatch delete SDR_abc123    # correct
lk sip dispatch delete --id SDR_abc  # WRONG — "flag provided but not defined: -id"
```

I've tripped on this twice. The `scripts/register-sip-routing.sh` uses
the positional form.

### The `_comment` field in JSON examples

LiveKit's proto doesn't define `_comment`, and recent CLI paths reject
unknown top-level fields while parsing request JSON. The repo's examples
can still use `_comment` for operator notes because
`scripts/register-sip-routing.sh` strips it before passing JSON to `lk`.

## References

- Proto: [livekit/protocol/protobufs/livekit_sip.proto](https://github.com/livekit/protocol/blob/main/protobufs/livekit_sip.proto)
- Matcher source: [livekit/protocol/sip/sip.go](https://github.com/livekit/protocol/blob/main/sip/sip.go) (`MatchDispatchRuleIter`)
- Inbound handler: [livekit/sip/pkg/sip/inbound.go](https://github.com/livekit/sip/blob/main/pkg/sip/inbound.go)
- LiveKit docs: https://docs.livekit.io/sip/dispatch-rule/
