# Twilio Elastic SIP Trunk setup

Step-by-step for configuring the Twilio side. Do this after
`livekit-sip` is running locally but before you point any real DIDs at
it — you'll want to test with a single number first.

**Authoritative sources:**

- [LiveKit's Twilio trunk docs](https://docs.livekit.io/telephony/start/providers/twilio/)
- [Twilio Elastic SIP Trunking](https://www.twilio.com/docs/sip-trunking)
- [Twilio SIP trunking IP addresses](https://www.twilio.com/docs/sip-trunking/ip-addresses)

This page mirrors the flow with notes specific to this voice stack.

## Prerequisites

- Twilio account with at least one DID you can spare for initial testing
- `livekit-sip` running and reachable on UDP 5060 from the public internet
- Your `livekit-sip` public hostname (DNS or raw IP). Twilio will open
  SIP sessions to this.
- `lk` CLI authenticated

## 1. Create the Elastic SIP Trunk

Twilio Console → **Elastic SIP Trunking** → **Trunks** → **Create new trunk**.

- **Friendly name:** `voice`
- Leave defaults; you'll fill in origination/termination below.

Save the **Trunk SID** — you'll reference it later.

## 2. Origination Connection Policy (inbound to LiveKit)

This tells Twilio: "when a call comes in on this trunk, forward it to
LiveKit over SIP."

Twilio Console → **Voice** → **Manage** → **Origination Connection
Policies** → **Create**.

- **Name:** `livekit-origination`
- **Target:** `sip:<your-public-livekit-sip-host>:5060;transport=tcp`
  - Use TCP, not UDP. UDP works but TCP is more reliable and handles
    large SIP INVITE bodies without fragmentation.
  - Example: `sip:voip.example.com:5060;transport=tcp`

Attach the policy to the trunk (Trunk → **Origination** → **Add Origination Connection Policy**).

## 3. Termination Credential List (outbound from LiveKit)

Even if you don't need outbound right now, create this up front so the
trunk is symmetric.

Twilio Console → **Voice** → **Manage** → **Credential Lists** → **Create**.

- **Friendly name:** `livekit-termination`
- Add one credential:
  - Username: `voice-sip` (pick something — you'll paste this into
    the livekit-sip outbound trunk config)
  - Password: generate a strong one; record it

Attach: Trunk → **Termination** → **Authentication** → check your new
credential list.

Record the **Termination SIP URI** from the trunk page. It looks like
`voice-xxxx.pstn.twilio.com`.

## 4. Attach a DID

Trunk → **Numbers** → **Add existing number** → pick a DID you're
willing to test on.

While the trunk is unattached, DIDs still hit Programmable Voice
webhooks (the current bridge path). Once you attach a number to the
trunk, inbound calls bypass Programmable Voice entirely and route to
the trunk's Origination URI.

**Important:** Test with a spare DID first. Move production numbers only
after inbound routing, dispatch rules, and a live call all pass.

## 5. Codec lock

Trunk → **Settings** → disable everything except **PCMU** (G.711 µ-law).

Why: Twilio will try Opus first if available, and codec renegotiation
during call setup can introduce one-way audio or delays. Force PCMU and
LiveKit SIP will transcode to Opus on the LiveKit side.

## 6. IP ACL (optional but recommended)

Trunk → **Settings** → **Authentication** → **IP ACLs**.

Add Twilio's public SIP ranges so `livekit-sip` only accepts INVITEs
from Twilio. Twilio publishes these; check
https://www.twilio.com/docs/sip-trunking/ip-addresses for the current
list.

Also: configure `livekit-sip` to set `allowed_addresses` on the inbound
trunk so it only accepts SIP from Twilio's SBC. Belt-and-suspenders.

## 7. Register the trunk with livekit-sip

Once Twilio side is done, register the inbound trunk with livekit-sip:

```bash
lk sip trunk create inbound \
  --name "twilio-primary" \
  --numbers "+1YOUR_DID_HERE" \
  --auth-user "voice-sip" \
  --auth-pass "<password-from-step-3>"
```

Save the returned trunk ID. It looks like `ST_xxxxxxxx`.

For outbound, register a separate outbound trunk pointing at Twilio's
termination SIP URI:

```bash
lk sip trunk create outbound \
  --name "twilio-outbound" \
  --address "voice-xxxx.pstn.twilio.com" \
  --numbers "+1YOUR_DID_HERE" \
  --auth-user "voice-sip" \
  --auth-pass "<password-from-step-3>"
```

## 8. Smoke test

Call the DID. Expected:

- Twilio Console → **Call Logs** shows the call hitting the trunk
- `livekit-sip` logs show SIP INVITE received
- LiveKit server logs show room creation
- If you have a dispatch rule set up (`make register-sip`), agent joins
  the room
- Without a dispatch rule, the call connects to an empty room — that's
  OK during first trunk validation. Register the dispatch rules next.

## Common failure modes

| Symptom                                     | Likely cause                                                  |
|---------------------------------------------|---------------------------------------------------------------|
| Twilio says 503 Service Unavailable         | livekit-sip not reachable on 5060, or IP ACL blocking         |
| Twilio says 401 Unauthorized on termination | Credential mismatch — check credential list + livekit-sip config |
| One-way audio                               | Codec mismatch or NAT issue — lock PCMU, check RTP port range |
| SIP connects, audio never starts            | RTP ports (10000-20000) blocked inbound                       |
| Connects fine but room never created        | Dispatch rule not configured or trunk ID mismatch             |

## What to record when you're done

Paste into `secrets/livekit-sip-trunk.md` (not this repo):

- Twilio trunk SID:
- Twilio termination SIP URI:
- SIP credential username / password:
- livekit-sip inbound trunk ID (`ST_...`):
- livekit-sip outbound trunk ID (`ST_...`):
- DID(s) attached:

You'll need these for `config/sip-*.json` and `make register-sip`.
