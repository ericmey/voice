# Bring Your Own Stack

This repository is a reference implementation for one OpenClaw LiveKit
voice deployment. The included personas, tools, and service names are
examples from that stack. Treat them as working samples you can replace,
not as a required application shape.

## External Projects

| Dependency | Used for | Reference |
| --- | --- | --- |
| OpenClaw | Gateway hooks and downstream agent work | <https://github.com/openclaw/openclaw> |
| Musubi | Cross-channel episodic memory and presence delivery | <https://github.com/ericmey/musubi> |
| OpenClaw OTel stack | Example Grafana/Loki/Tempo/Mimir support stack | <https://github.com/ericmey/openclaw-otel> |
| LiveKit Agents | Python voice-agent runtime | <https://docs.livekit.io/agents/> |
| LiveKit SIP | SIP bridge into LiveKit rooms | <https://docs.livekit.io/sip/> |

OpenClaw and Musubi are not vendored here. If you use different agent,
memory, or gateway systems, replace the small integration surfaces rather
than copying the whole stack.

## Configuration Map

| Area | Required? | Primary files/env | Replace with your own |
| --- | --- | --- | --- |
| LiveKit server + SIP + Redis | Yes | `docker-compose.yaml`, `config/livekit*.yaml` | Your LiveKit deployment or hosted LiveKit credentials |
| SIP trunk + dispatch rules | Yes for phone calls | `config/sip-*.json`, `docs/twilio-trunk.md` | Twilio, Telnyx, carrier SBC, or any LiveKit SIP-compatible provider |
| Agents/personas | Yes | `agents/*/src/agent.py`, `agents/*/prompts/system.md` | Your own agent packages, names, voices, prompts, model choices |
| Tools | Optional but useful | `tools/src/tools/`, `tools/README.md` | Your own LiveKit `@function_tool` mixins |
| OpenClaw delegation | Optional | `OPENCLAW_HOOK_*`, `sdk/src/sdk/openclaw_hooks.py`, `tools/src/tools/sessions.py` | Another async job API, webhook receiver, queue, or no delegation |
| Musubi memory | Optional | `MUSUBI_V2_*`, `sdk/src/sdk/musubi_v2_client.py`, `tools/src/tools/memory.py` | Another memory API, local store, or remove memory tools |
| Observability | Recommended | `OPENCLAW_OTLP_*`, `docs/OBSERVABILITY.md` | Grafana, Honeycomb, Datadog, New Relic, or any OTLP/HTTP backend |
| Agent lifecycle | Required on macOS | `config/launchd/`, `scripts/deploy-agents.sh`, `scripts/cycle-agents.sh` | systemd, containers, Nomad, Kubernetes, or another supervisor |

## Replacing Agents

The checked-in agents are samples:

- `nyla`, `aoi`, and `yua` use Gemini native audio through LiveKit Agents.
- `party` uses a chained STT/LLM/TTS pipeline.

To add your own:

1. Copy one `agents/<name>/` package or create a new uv workspace member.
2. Change the prompt, `AgentConfig`, model/voice builder, and tool mixins.
3. Add the member to the root `pyproject.toml`.
4. Add launchd/secrets mapping in `scripts/deploy-agents.sh` if the agent
   needs different tokens or environment.
5. Add a SIP dispatch JSON example if it should receive phone calls.

For a fully generic template, replace the `nyla`, `aoi`, `yua`, and `party`
names across `agents/`, `config/sip-dispatch-*.json.example`, and the
deploy script mappings.

## Replacing Tools

Tools are regular mixins with LiveKit `@function_tool` methods. The voice
model only sees decorated methods composed into the concrete agent class.

To add or replace tools:

1. Add a module under `tools/src/tools/`.
2. Export the mixin in `tools/src/tools/__init__.py`.
3. Compose it into your agent class.
4. Update [tools/README.md](../tools/README.md).
5. Test with `make voice-harness` before making a live call.

Keep phone-call tools fast. Prefer accepted-and-returning async handoff
patterns for long-running work, then let the downstream agent or service
deliver results through its normal channel.

## Replacing OpenClaw Delegation

The default tool surface uses `openclaw_delegate`, which posts to
OpenClaw Gateway hooks and returns after the Gateway accepts the request.
That avoids blocking a phone conversation while the downstream agent does
the real work.

If you do not use OpenClaw:

- replace `sdk/src/sdk/openclaw_hooks.py` with your webhook/queue client;
- replace `SessionsToolsMixin.openclaw_delegate`;
- update prompts so the model knows when to use your async handoff;
- update `docs/VOICE-TOOL-HARNESS.md` or the harness cases.

Avoid making the phone agent duplicate your backend agent's routing,
tools, and delivery rules. The voice agent should be a conversational
front door, not a second implementation of the same persona.

## Replacing Musubi Memory

Musubi is used for episodic memory and presence-to-presence delivery. If
you do not run Musubi, remove `MusubiToolsMixin` from agent inheritance or
replace `sdk/src/sdk/musubi_v2_client.py` with your memory client.

Memory tools are optional. The core phone stack can still answer calls,
delegate work, log transcripts, and export telemetry without Musubi.

## Telemetry Requirements

The agents emit OTLP/HTTP traces, logs, and metrics. You need one of:

- an OpenTelemetry Collector or Grafana Alloy receiving OTLP/HTTP;
- a hosted OTLP endpoint such as Grafana Cloud, Honeycomb, Datadog, or
  another vendor;
- local collector endpoints for development.

The app-side endpoint must include the OTLP signal path:

```bash
OPENCLAW_OTLP_ENDPOINT=http://localhost:4318/v1/traces
```

When explicit log/metric endpoints are unset, the SDK derives
`/v1/logs` and `/v1/metrics` from the traces endpoint. See
[OBSERVABILITY.md](OBSERVABILITY.md) for details.

Useful public references:

- OpenTelemetry Collector: <https://opentelemetry.io/docs/collector/>
- OTLP protocol: <https://opentelemetry.io/docs/specs/otlp/>
- Grafana Cloud OTLP: <https://grafana.com/docs/grafana-cloud/send-data/otlp/>
- Grafana OpenTelemetry Collector setup:
  <https://grafana.com/docs/opentelemetry/collector/opentelemetry-collector/>

## SIP Provider Notes

This repo's phone path is documented with Twilio Elastic SIP Trunking,
but LiveKit SIP can work with other SIP providers. If you use Twilio,
start with [twilio-trunk.md](twilio-trunk.md) and compare against the
official docs:

- Twilio Elastic SIP Trunking: <https://www.twilio.com/docs/sip-trunking>
- Twilio SIP trunking IP addresses:
  <https://www.twilio.com/docs/sip-trunking/ip-addresses>
- LiveKit Twilio provider guide:
  <https://docs.livekit.io/telephony/start/providers/twilio/>

For any provider, confirm SIP signaling reachability, RTP port exposure,
codec compatibility, and dispatch-rule matching before moving production
numbers.
