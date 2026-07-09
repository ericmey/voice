# tools/ — voice-agent tool catalog

Browseable list of every `@function_tool` available to the voice agents.
Each tool lives in a **mixin class**; agents compose the mixins they want
in their `__mro__`:

```python
class NylaAgent(HouseholdToolsMixin, BaseRealtimeAgent):  # Base = Core + Musubi
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
| `musubi_get` | [memory.py](src/tools/memory.py) | `MusubiToolsMixin` | Fetch one Musubi object by id. Deferred stub — the name is reserved and it returns a "not yet available" message pointing back at `musubi_search` until the client gains per-plane `.get()`. | `plane`, `namespace`, `object_id` |
| `household_status` | [household.py](src/tools/household.py) | `HouseholdToolsMixin` | Read-only survey of what *other* household agents have been doing (fans out over `AgentConfig.household_presences`) | `hours=24`, `limit=15` |

## Mixins and who uses them

| Mixin | Agents that compose it |
|---|---|
| `CoreToolsMixin` | nyla, aoi, yua, party |
| `MusubiToolsMixin` | nyla, aoi, yua, party |
| `MemoryToolsMixin` | Back-compat alias for `MusubiToolsMixin` |
| `HouseholdToolsMixin` | nyla, aoi, yua |

Nyla, Aoi, and Yua subclass `BaseRealtimeAgent` (which already composes
`CoreToolsMixin` + `MusubiToolsMixin`) and add `HouseholdToolsMixin`.
Party composes `CoreToolsMixin` + `MusubiToolsMixin` directly and does
not survey the household.

## Musubi Canonical API

`MusubiToolsMixin` is the live memory surface. It talks to the canonical
Musubi HTTP API (`MUSUBI_V2_BASE_URL`, default `http://localhost:8100/v1`)
with bearer auth. Agents read from `<tenant>/*/episodic` for cross-channel
recall and write to their own `<agent>/<channel>/episodic` namespace.

`MemoryToolsMixin` remains as a temporary import alias for older code paths.

## No delegation

The voice agents are standalone: they answer the phone and read/write
Musubi memory directly. There is no delegation surface — an agent cannot
hand work to another agent, spawn a session, or schedule a callback. Any
side effect a tool has is a direct Musubi call (or, for
`household_status`, a read-only fan-out over Musubi).

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

- `config.agent_name` — "nyla" | "aoi" | "yua" | "party" — used for
  self-reference in prompts and memory tagging.
- `config.memory_agent_tag` — tag used when storing memories so recall
  can filter per-agent.
- `config.musubi_v2_namespace` / `config.musubi_v2_presence` — the
  two-segment `<agent>/<channel>` prefix that scopes memory reads/writes
  and thought sends.
- `config.household_presences` — the presences this agent may survey via
  `household_status` (empty tuple = no household-wide visibility).

Agents set `config` as a class attribute in their `_shared.py`; the
mixin's method body reads `self.config.X` polymorphically.
