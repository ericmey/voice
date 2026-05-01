# tools/ — voice-agent tool catalog

Browseable list of every `@function_tool` available to the voice agents.
Each tool lives in a **mixin class**; agents compose the mixins they want
in their `__mro__`:

```python
class NylaAgent(CoreToolsMixin, MusubiToolsMixin, SessionsToolsMixin,
                AcademyToolsMixin, Agent):
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
| `musubi_think` | [memory.py](src/tools/memory.py) | `MusubiToolsMixin` | Presence-to-presence thought delivery (canonical API) | `to_agent`, `content`, `channel="default"` |
| `sessions_send` | [sessions.py](src/tools/sessions.py) | `SessionsToolsMixin` | Send a task/message to another AI agent | `agent_id`, `message`, `deliver_to="room"` |
| `sessions_spawn` | [sessions.py](src/tools/sessions.py) | `SessionsToolsMixin` | Spawn a new agent session to handle a task | `agent_id`, `task`, `deliver_to="room"` |
| `academy_selfie` | [academy.py](src/tools/academy.py) | `AcademyToolsMixin` | Request a selfie of the speaking agent from Mizuki | `mood`, `nsfw=False` |
| `academy_send` | [academy.py](src/tools/academy.py) | `AcademyToolsMixin` | Request an image of any character from Mizuki | `character`, `prompt`, `rating="general"` |

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
| `AcademyToolsMixin` | nyla, aoi, party |

## Musubi Canonical API

`MusubiToolsMixin` is the live memory surface. It talks to the canonical
Musubi HTTP API (`MUSUBI_V2_BASE_URL`, default `http://localhost:8100/v1`)
with bearer auth. Agents read from `<tenant>/*/episodic` for cross-channel
recall and write to their own `<agent>/<channel>/episodic` namespace.

`MemoryToolsMixin` remains as a temporary import alias for older code paths.

## How tools reach side effects

Actuator-shaped tools (Discord messaging, image generation, delegation)
don't talk to Discord / ComfyUI / cron directly. They spawn the OpenClaw
CLI via [`sdk.cli_spawner.fire_and_forget_async`](../sdk/src/sdk/cli_spawner.py)
from voice-tool coroutines, with an explicit argv — safe, testable, no
shell-injection surface and no synchronous fork on the voice event loop.

The `OPENCLAW_VOICE_TOOLS_DRY_RUN=1` env var short-circuits every spawn
to a logged no-op — lets tests exercise the full tool path without firing
real Discord messages or kicking off real agent sessions.

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
  `sessions_send` / `sessions_spawn` (None = no restriction).
- `config.memory_agent_tag` — tag used when storing memories so recall
  can filter per-agent.

Agents set `config` as a class attribute in their `_shared.py`; the
mixin's method body reads `self.config.X` polymorphically.
