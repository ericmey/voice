# tools/ — voice-agent tool catalog

Browseable list of every `@function_tool` available to the voice agents.
Each tool lives in a **mixin class**; agents compose the mixins they want
in their `__mro__`:

```python
class NylaAgent(CoreToolsMixin, MusubiToolsMixin, SessionsToolsMixin, Agent):
    ...
```

LiveKit discovers `@function_tool`-decorated methods via MRO walk and
exposes them to the voice model as callable tools.

## Catalog

| Tool | Module | Mixin | Description | Args |
|---|---|---|---|---|
| `get_current_time` | [core.py](src/tools/core.py) | `CoreToolsMixin` | Current local date + time on the server | — |
| `get_weather` | [core.py](src/tools/core.py) | `CoreToolsMixin` | Current weather in Carmel, IN | — |
| `musubi_recent` | [memory.py](src/tools/memory.py) | `MusubiToolsMixin` | Recent voice-channel memories (recency-ordered, agent-tag filtered) | `limit=10` |
| `musubi_search` | [memory.py](src/tools/memory.py) | `MusubiToolsMixin` | Cross-channel hybrid retrieve (`<tenant>/*/episodic`, deep mode, includes provisional) | `query`, `limit=5` |
| `musubi_remember` | [memory.py](src/tools/memory.py) | `MusubiToolsMixin` | Persist a memory for future recall (canonical Musubi episodic) | `content`, `topics=[]`, `importance=7` |
| `musubi_think` | [memory.py](src/tools/memory.py) | `MusubiToolsMixin` | Presence-to-presence thought delivery (canonical API) | `to_presence`, `content`, `channel="default"` |
| `openclaw_delegate` | [sessions.py](src/tools/sessions.py) | `SessionsToolsMixin` | Fire-and-forget work to an OpenClaw agent via Gateway hooks | `agent_id`, `task`, `deliver_to="room"` |

### Disabled but preserved

| Tool | Module | Status |
|---|---|---|
| `schedule_callback` | [sessions.py](src/tools/sessions.py) | `@function_tool` decorator removed so the model can't call it. Method body + validation + tests preserved for re-enable. See [../sdk/TODO.md](../sdk/TODO.md) for the preferred cron-payload redesign. |

## Mixins and who uses them

| Mixin | Agents that compose it |
|---|---|
| `CoreToolsMixin` | nyla, aoi, party |
| `MusubiToolsMixin` | nyla, aoi, party |
| `MemoryToolsMixin` | Back-compat alias for `MusubiToolsMixin` |
| `SessionsToolsMixin` | nyla, aoi, party |

## Musubi Canonical API

`MusubiToolsMixin` is the live memory surface. It talks to the canonical
Musubi HTTP API (`MUSUBI_V2_BASE_URL`, default `http://localhost:8100/v1`)
with bearer auth. Agents read from `<tenant>/*/episodic` for cross-channel
recall and write to their own `<agent>/<channel>/episodic` namespace.

`MemoryToolsMixin` remains as a temporary import alias for older code paths.

## How tools reach side effects

Delegation does not talk to Discord, Mizuki, or OpenClaw agent internals
directly. `openclaw_delegate` posts to the Gateway `/hooks/agent` endpoint
via [`sdk.openclaw_hooks`](../sdk/src/sdk/openclaw_hooks.py), then returns
once OpenClaw accepts the request. The target OpenClaw agent owns the work
and normal delivery behavior.

Configure `OPENCLAW_HOOK_TOKEN` with a dedicated Gateway hook token.
`OPENCLAW_GATEWAY_HTTP_URL` defaults to `http://127.0.0.1:$GATEWAY_PORT`
when unset, and `OPENCLAW_HOOKS_PATH` defaults to `/hooks`.

The remaining CLI spawner is preserved only for disabled callback code
while the callback redesign is pending.

## Testing without a phone call

Run `make voice-harness` to instantiate the real agent class and exercise
`openclaw_delegate` in mock mode. The harness verifies the model-visible
tool surface and prints the Gateway hook payload that would be submitted.
See [docs/VOICE-TOOL-HARNESS.md](../docs/VOICE-TOOL-HARNESS.md).

## Adding a new tool

1. Either extend an existing mixin with a new `@function_tool`-decorated
   method, or drop a new module into `src/tools/<name>.py` with its own
   mixin class.
2. Export the mixin from [src/tools/__init__.py](src/tools/__init__.py).
3. Add a row to the catalog table above.
4. Update the "Mixins and who uses them" table if it's a new mixin class.
5. `make verify` to confirm the change is lint/type/test clean.

## Per-agent customization

Each tool reads `self.config` (an `AgentConfig` instance set by the
concrete agent class) for per-agent behavior:

- `config.agent_name` — "nyla" | "aoi" | "party" — used for self-reference
  in prompts and as the `--agent` slot in CLI spawns.
- `config.discord_room` — default target for `deliver_to="room"`.
- `config.allowed_delegation_targets` — optional allowlist for
  `openclaw_delegate` (None = no restriction).
- `config.memory_agent_tag` — tag used when storing memories so recall
  can filter per-agent.

Agents set `config` as a class attribute in their `_shared.py`; the
mixin's method body reads `self.config.X` polymorphically.
