# Magpie Zero Shot production deployment

This is Mizuki's production TTS stack. It has two services:

- `magpie-tts-zeroshot`: NVIDIA NIM/Riva on GPU 0, pinned by image digest;
- `magpie-voice-registry`: the stable named-voice HTTP contract used by fleet
  tools and Hermes integrations.

Sumi's LiveKit worker uses the NIM's native gRPC endpoint directly and keeps her
prompt and quality setting in Compose. Other callers use the registry so names
such as `yua-v1`, `nyla-v2`, and `sumi-v1` remain reusable voice identities.

## Model artifacts

The approved NIM was exported once as an RMIR and starts with model downloads
disabled. Runtime therefore requires no NGC token.

| Artifact | Mizuki path | SHA-256 |
| --- | --- | --- |
| Original NVIDIA export | `/home/ericmey/voice/magpie-export/magpie-tts-zeroshot-sm120-bs8.tar.gz` | `c8249bf5162e183e4e6d027abd973a542a3e444c58b13b56543ef9b3fae7454a` |
| Production export | `/home/ericmey/voice/magpie-export-clean/magpie-tts-zeroshot-sm120-bs8-no-stale-context-path.tar.gz` | `f210b21376abb984b3e518dd0c6e437aa5d20b6652784184d4da6d1fdbe26d5f` |

The production copy differs only by clearing the generated RMIR's stale
`context_encoder_path`. No arbitrary engine was substituted: this approved
profile routes prompt conditioning through its present `codec_encoder.plan`.
The original export is retained unchanged for inspection and rollback.

## Endpoints and contract

- registry HTTP: `10.0.20.25:5056`
- NIM HTTP: `10.0.20.25:9101`
- NIM gRPC: `10.0.20.25:51052`
- native output: signed 16-bit mono PCM at 22,050 Hz
- Zero Shot quality: 40
- admitted concurrency: 8
- GPU: device 0, Blackwell `sm_120`

The prompt registry is deployed at
`/home/ericmey/voice/magpie-registry/registry.json`; prompt WAVs are under
`/home/ericmey/voice/voice-prompts`. Registry startup validates that every
prompt is mono signed-16-bit 22.05 kHz audio, peaks at -12 dBFS, and matches its
declared SHA-256.

## Start and verify

From `/home/ericmey/voice` on Mizuki:

```sh
docker compose -p magpie -f deploy/magpie/docker-compose.nim.yaml \
  -f deploy/magpie/docker-compose.registry.yaml up -d
curl -fsS http://10.0.20.25:5056/healthz
curl -fsS http://10.0.20.25:5056/voices
./scripts/health-check.sh
```

A true cold NIM start measured 68 seconds on the RTX 5060 Ti. The healthcheck's
ten-minute start period is deliberate headroom for engine loading, not an
expected outage.

## Rollback

The pre-cutover Qwen stack is preserved at
`/Users/ericmey/Backups/voice-qwen-20260729T150040Z` on the operator Mac, and
the stopped Qwen container plus rollback image remain on Mizuki. Do not delete
either until Magpie has accumulated enough production calls to close the
rollback window.

To inspect or retry the unmodified NVIDIA RMIR, change only the NIM volume from
`magpie-export-clean` to `magpie-export`; do not overwrite either export tree.
