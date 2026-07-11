# tools/ — voice-agent tool catalog

Every `@function_tool` the voice agents can call. **This table is enforced**: 
`tests/test_catalog_cannot_lie.py` checks it against the real `@function_tool` methods on
the real mixins, in both directions. Add a tool and the tests fail until you document it;
delete one and they fail until you remove the row.

That guard exists because this file used to lie. On 2026-07-11 it documented eight tools;
five existed. `musubi_think` (un-registered 2026-07-10), `musubi_get` (removed 2026-07-09),
and `household_status` — from a module, `household.py`, that does not exist. Neither does
`HouseholdToolsMixin`, so the worked example below would not even import. AGENTS.md names
this file as the authoritative catalog, which made it a false map at the top of the tree.

## Composition

Each tool lives in a **mixin**. Every agent states her own composition — the base class does
NOT decide it for her (see `tools/base_agent.py`, and `tests/test_composition_is_explicit.py`):

```python
class NylaAgent(CoreToolsMixin, MusubiToolsMixin, BaseRealtimeAgent): ...
class SumiAgent(CoreToolsMixin, MusubiToolsMixin, BaseVoiceAgent):    ...  # chained pipeline
```

Adding a capability to one agent — Nyla's Hermes tools, Aoi's Claude Code channel, Yua's
Codex channel — means adding a mixin to *her* class. Not to a base that would hand it to
everyone.

LiveKit discovers `@function_tool` methods by walking the MRO and exposes them to the model.

## Catalog

| Tool | Module | Mixin | Description | Args |
|---|---|---|---|---|
| `get_current_time` | [core.py](src/tools/core.py) | `CoreToolsMixin` | Current local date + time on the server | — |
| `get_weather` | [core.py](src/tools/core.py) | `CoreToolsMixin` | Current weather in Carmel, IN | — |
| `musubi_recent` | [memory.py](src/tools/memory.py) | `MusubiToolsMixin` | Recent voice-channel memories (recency-ordered, agent-tag filtered) | `limit=10` |
| `musubi_search` | [memory.py](src/tools/memory.py) | `MusubiToolsMixin` | Cross-channel hybrid retrieve (`<tenant>/*/episodic`, deep mode, includes provisional) | `query`, `limit=5` |
| `musubi_remember` | [memory.py](src/tools/memory.py) | `MusubiToolsMixin` | Persist a memory for future recall (canonical Musubi episodic) | `content`, `topics=[]`, `importance=7` |

## Retired

Kept here deliberately — a deleted tool that vanishes without trace gets re-proposed.

| Tool | Retired | Why |
|---|---|---|
| `musubi_think` | 2026-07-10 | Presence-to-presence thought delivery. It contradicted every persona (none of them talk *about* messaging each other mid-call) and wrote to a plane nobody reads. The client method `MusubiClient.send_thought` remains for programmatic use. |
| `musubi_get` | 2026-07-09 | A deferred stub that only ever returned "not yet available". A tool that cannot do the thing is a tool the model will still try to call. |
| `household_status` | pre-2026-07 | Fan-out over other agents' Musubi presences. The module (`household.py`), the mixin (`HouseholdToolsMixin`), and the config fields it read (`household_presences`) are all gone. |

## Mixins and who uses them

| Mixin | Agents that compose it |
|---|---|
| `CoreToolsMixin` | nyla, aoi, yua, sumi |
| `MusubiToolsMixin` | nyla, aoi, yua, sumi |

Nyla, Aoi, and Yua subclass `BaseRealtimeAgent` (which already composes
Sumi composes `CoreToolsMixin` + `MusubiToolsMixin` directly and does

## Musubi Canonical API

`MusubiToolsMixin` is the live memory surface. It talks to the canonical
Musubi HTTP API (`MUSUBI_V2_BASE_URL`, default `http://localhost:8100/v1`)
with bearer auth. Agents read from `<tenant>/*/episodic` for cross-channel
recall and write to their own `<agent>/<channel>/episodic` namespace.


## No delegation

The voice agents are standalone: they answer the phone and read/write
Musubi memory directly. There is no delegation surface — an agent cannot
hand work to another agent, spawn a session, or schedule a callback. Any
side effect a tool has is a direct Musubi call (or, for

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

- `config.agent_name` — "nyla" | "aoi" | "yua" | "sumi" — used for
  self-reference in prompts and memory tagging.
- `config.memory_agent_tag` — tag used when storing memories so recall
  can filter per-agent.
- `config.musubi_v2_namespace` / `config.musubi_v2_presence` — the
  two-segment `<agent>/<channel>` prefix that scopes memory reads/writes
  and thought sends.

Agents set `config` as a class attribute in their `_shared.py`; the
mixin's method body reads `self.config.X` polymorphically.