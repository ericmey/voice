# voicebook-tts — deployed state

What is actually running, with hashes. Host-local edits are not the artifact;
this directory is.

## Service — mizuki (10.0.20.25)

| field | value |
|---|---|
| image | `voicebook-tts:cu128` |
| image id | `sha256:a78a39d13bca85144e4d9726b22653c701d516c4987a76a4d6db1aaecfbb12aa` |
| bind | `10.0.20.25:5055` — **private LAN interface only, never 0.0.0.0** |
| model | pinned snapshot `fd4b254389122332181a7c3db7f27e918eec64e3` |
| masters | `/srv/voicebook/{sumi,nyla}/` root:root `0444`, mounted `:ro` |
| registry | `/srv/voicebook/registry.json` root:root `0444`, mounted `:ro` |

**Why the LAN bind, not loopback.** Hermes runs on nyla.mey.house; a loopback
bind is unreachable from it. That was found by an actual failed curl from nyla,
not by reasoning.

## Access control — `voicebook-fw.service`

The bind alone is not access control. A private-VLAN bind exposes an
**unauthenticated custom-voice synthesis API to every VLAN-20 host** — anyone on
the LAN could generate audio in either girl's voice.

`voicebook-fw.service` (in this directory) installs a source allowlist in the
`DOCKER-USER` chain — `INPUT` is bypassed by Docker's publish path, so the rule
must live there. Only `10.0.20.20` may reach `:5055`.

Idempotent (deletes before inserting), enabled at boot, and `ExecStop` removes
the rules — which is the rollback.

**Proven both ways:** nyla returns `healthz`; momo, another VLAN-20 host, is
blocked.

## Hermes adapter — nyla.mey.house

`~/.hermes/bin/voicebook-tts.py`
SHA-256 `eac51193a0e2bef482ba89eeb9dc0c825815e3b355f4f3bfe7468143c01b81f1`
(byte-identical copy in this directory as `hermes-adapter-voicebook-tts.py`)

Sends `voice_id` and nothing else — never a path, never a hash. **No fallback
path exists in the code.** Red-proofed before wiring:

| case | result |
|---|---|
| service unreachable | exit 1, **no file written** |
| unknown `voice_id` | exit 1, **no file written** |
| success | exit 0, valid WAV |

## Girl configs — nyla.mey.house

| profile | change | pre-change SHA-256 |
|---|---|---|
| `sumi` | `orpheus` → `voicebook`, `voice_id: sumi-v1` | `ac6cb881b62597fad3cd1e4d80be3df248f5893b98e5f2caab585f6720501025` |
| `nyla` | `elevenlabs` → `voicebook`, `voice_id: nyla-v1` | `58b77e647bcd5890ee574429442e3442ba976c42bc2f05f91f82582dbab5cde7` |

Backups: `config.yaml.bak-qwen-cutover-20260722` alongside each.

**ElevenLabs was removed from both, not left inert.** Nyla was still actively on
it. Neither girl now has any cloud TTS in her config — there is nothing to fall
back to, by construction.

## Rollback

1. Restore `config.yaml.bak-qwen-cutover-20260722` for either girl.
2. `systemctl stop voicebook-fw` removes the allowlist.
3. `docker rm -f voicebook-tts` stops the service.
4. Orpheus is preserved and reversible: image `orpheus-fastapi:cu128-blackwell`,
   project tree, branch `harem/sm120-blackwell`, and the patch export.

## Not proven

Discord end-to-end (manual adapter renders prove Hermes-command → service, not a
real Discord request). Long-form tail coverage. Repeatability, concurrency
beyond the single-flight guard, and expressive range.

**"No cloud in the path" is earned for the TTS synthesis leg only.** The LLM and
tool paths are a separate question and are not addressed here.
