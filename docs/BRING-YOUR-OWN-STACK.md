# Bring Your Own Stack

This repository is a reference implementation for one LiveKit voice
deployment. The included personas, tools, and service names are
examples from that stack. Treat them as working samples you can replace,
not as a required application shape.

## External Projects

| Dependency | Used for | Reference |
| --- | --- | --- |
| Musubi | Cross-channel episodic memory and presence delivery | <https://github.com/ericmey/musubi> |
| LiveKit Agents | Python voice-agent runtime | <https://docs.livekit.io/agents/> |
| LiveKit SIP | SIP bridge into LiveKit rooms | <https://docs.livekit.io/sip/> |

Musubi is not vendored here. If you use a different memory or presence
system, replace the small integration surface rather than copying the
whole stack.

## Configuration Map

| Area | Required? | Primary files/env | Replace with your own |
| --- | --- | --- | --- |
| LiveKit server + SIP + Redis | Yes | `docker-compose.yaml`, `config/livekit*.yaml` | Your LiveKit deployment or hosted LiveKit credentials |
| SIP trunk + dispatch rules | Yes for phone calls | `config/sip-*.json`, `docs/twilio-trunk.md` | Twilio, Telnyx, carrier SBC, or any LiveKit SIP-compatible provider |
| Agents/personas | Yes | `agents/*/src/agent.py`, `agents/*/prompts/system.md` | Your own agent packages, names, voices, prompts, model choices |
| Tools | Optional but useful | `tools/src/tools/`, `tools/README.md` | Your own LiveKit `@function_tool` mixins |
| Musubi memory | Optional | `MUSUBI_V2_*`, `sdk/src/sdk/musubi_client.py`, `tools/src/tools/memory.py` | Another memory API, local store, or remove memory tools |
| Observability | Recommended | `VOICE_OTLP_*`, `docs/OBSERVABILITY.md` | Grafana, Honeycomb, Datadog, New Relic, or any OTLP/HTTP backend |
| Agent lifecycle | Yes | `docker-compose.agents.yaml`, `Dockerfile.agent` | systemd, Nomad, Kubernetes, or another container/process supervisor |

## Replacing Agents

The checked-in agents are samples:

- `nyla`, `aoi`, and `yua` use Gemini native audio through LiveKit Agents.
- `sumi` uses a fully-local chained STT/LLM/TTS pipeline (Riva/Nemo/Orpheus).

To add your own:

1. Copy one `agents/<name>/` package or create a new uv workspace member.
2. Change the prompt, `AgentConfig`, model/voice builder, and tool mixins.
3. Add the member to the root `pyproject.toml`.
4. Add a service for it in `docker-compose.agents.yaml` (and a
   `MUSUBI_V2_TOKEN_<AGENT>` in `secrets/livekit-agents.env`) if the
   agent needs different tokens or environment.
5. Add a SIP dispatch JSON example if it should receive phone calls.

For a fully generic template, replace the `nyla`, `aoi`, `yua`, and `sumi`
names across `agents/`, `config/sip-dispatch-*.json.example`, and
`docker-compose.agents.yaml`.

## Replacing Tools

Tools are regular mixins with LiveKit `@function_tool` methods. The voice
model only sees decorated methods composed into the concrete agent class.

To add or replace tools:

1. Add a module under `tools/src/tools/`.
2. Export the mixin in `tools/src/tools/__init__.py`.
3. Compose it into your agent class.
4. Update [tools/README.md](../tools/README.md).
5. `make verify` before making a live call.

Keep phone-call tools fast. The agent has no delegation surface — every
tool runs to completion on the call — so long-running work does not
belong in a tool. If you need async handoff, add it as a new integration
surface of your own.

## Replacing Musubi Memory

Musubi is used for episodic memory and presence-to-presence delivery. If
you do not run Musubi, remove `MusubiToolsMixin` from agent inheritance or
replace `sdk/src/sdk/musubi_client.py` with your memory client.

Memory tools are optional. The core phone stack can still answer calls,
log transcripts, and export telemetry without Musubi.

## Telemetry Requirements

The agents emit OTLP/HTTP traces, logs, and metrics. You need one of:

- an OpenTelemetry Collector or Grafana Alloy receiving OTLP/HTTP;
- a hosted OTLP endpoint such as Grafana Cloud, Honeycomb, Datadog, or
  another vendor;
- local collector endpoints for development.

The app-side endpoint must include the OTLP signal path:

```bash
VOICE_OTLP_ENDPOINT=http://localhost:4318/v1/traces
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
