"""Tests for MusubiToolsMixin — canonical agent-tools surface.

Covers musubi_recent, musubi_search, musubi_remember, musubi_think,
musubi_get + the fetch_recent_context helper. Also asserts the
``MemoryToolsMixin`` deprecation alias still resolves to the canonical
class for one release.
"""

import asyncio
from typing import Any, cast

import pytest
from sdk.config import NYLA_DEFAULT_CONFIG, AgentConfig
from tools.memory import MemoryToolsMixin, MusubiToolsMixin


def test_memory_mixin_alias_resolves_to_musubi_tools_mixin() -> None:
    """``MemoryToolsMixin`` is a one-release deprecation alias per
    Musubi ADR 0032. Until the alias is removed, importers must keep
    landing on the canonical class so behavior stays identical."""
    assert MemoryToolsMixin is MusubiToolsMixin


def _unwrap(tool: Any) -> Any:
    """LiveKit's `function_tool` wraps the underlying coroutine in a
    ``FunctionTool`` whose declared interface doesn't expose
    ``__wrapped__``, but the runtime always sets it (functools.wraps).
    Cast through ``Any`` so pyright doesn't complain on the test side
    while still asserting on real wire shape."""
    return cast(Any, tool).__wrapped__


def test_memory_mixin_has_musubi_recent():
    assert hasattr(MemoryToolsMixin, "musubi_recent")
    assert callable(MemoryToolsMixin.musubi_recent)


def test_memory_mixin_has_musubi_search():
    assert hasattr(MemoryToolsMixin, "musubi_search")
    assert callable(MemoryToolsMixin.musubi_search)


def test_memory_mixin_has_musubi_remember():
    assert hasattr(MemoryToolsMixin, "musubi_remember")
    assert callable(MemoryToolsMixin.musubi_remember)


def test_memory_mixin_exposes_fetch_recent_context_helper():
    """The plain-async helper used by on_enter must exist and be callable
    without the function_tool wrapping that musubi_recent carries."""
    assert hasattr(MemoryToolsMixin, "fetch_recent_context")
    assert callable(MemoryToolsMixin.fetch_recent_context)


def test_memory_mixin_default_config_is_nyla():
    """Absent an override, stored memories are tagged as Nyla's."""
    assert MemoryToolsMixin.config is NYLA_DEFAULT_CONFIG
    assert MemoryToolsMixin.config.memory_agent_tag == "nyla-voice"


def test_memory_mixin_config_is_overridable():
    """A subclass can point config at a different AgentConfig."""
    aoi_cfg = AgentConfig(
        agent_name="aoi",
        memory_agent_tag="aoi-voice",
        discord_room="channel:0",
    )

    class _AoiMemory(MemoryToolsMixin):
        config = aoi_cfg

    assert _AoiMemory.config.memory_agent_tag == "aoi-voice"
    # Parent class unaffected.
    assert MemoryToolsMixin.config.memory_agent_tag == "nyla-voice"


def test_composed_agent_has_memory_tools(agent):
    """Memory tools are discoverable on a composed agent instance."""
    assert hasattr(agent, "musubi_recent")
    assert hasattr(agent, "musubi_search")
    assert hasattr(agent, "musubi_remember")
    assert hasattr(agent, "fetch_recent_context")
    # Default composed agent doesn't override, so tag is "nyla-voice".
    assert agent.config.memory_agent_tag == "nyla-voice"


@pytest.mark.asyncio
async def test_fetch_recent_context_has_aggregate_timeout(agent, monkeypatch):
    async def slow_scroll(*args, **kwargs):
        await asyncio.sleep(0.05)
        return []

    agent._scroll_episodic_recent = slow_scroll
    monkeypatch.setattr("tools.memory._RECENT_CONTEXT_TIMEOUT_S", 0.001)

    result = await agent.fetch_recent_context(limit=10)

    assert "Musubi is unavailable" in result


# ---------------------------------------------------------------------------
# musubi_search behaviour — namespace shape, state_filter, mode
# ---------------------------------------------------------------------------


class _StubClient:
    """Records a single retrieve() call so tests can assert the wire shape
    without standing up a real Musubi server. Mirrors `MusubiV2Client.retrieve`
    keyword arguments exactly so signature drift breaks the test."""

    def __init__(self, response: dict[str, Any] | None = None) -> None:
        self._response = response or {"results": []}
        self.calls: list[dict[str, Any]] = []

    async def retrieve(
        self,
        *,
        namespace: str,
        query_text: str,
        mode: str = "fast",
        limit: int = 10,
        planes: list[str] | None = None,
        include_archived: bool = False,
        state_filter: list[str] | None = None,
        session: object | None = None,
    ) -> dict[str, Any]:
        self.calls.append(
            {
                "namespace": namespace,
                "query_text": query_text,
                "mode": mode,
                "limit": limit,
                "planes": planes,
                "include_archived": include_archived,
                "state_filter": state_filter,
            }
        )
        return self._response


@pytest.mark.asyncio
async def test_musubi_search_uses_tenant_wildcard_namespace(agent):
    """`musubi_search` must use `<tenant>/*/episodic` so cross-channel
    recall works (per Musubi ADR 0031). A regression to the agent's own
    channel breaks the multimodality contract — phone Nyla would stop
    seeing Discord-Nyla's deliberate stores."""
    stub = _StubClient(response={"results": []})
    agent._musubi_v2_client = lambda: stub
    # Force a known 2-segment presence so the test isn't sensitive to fixture defaults.
    agent.config = AgentConfig(
        agent_name="nyla",
        memory_agent_tag="nyla-voice",
        discord_room="channel:0",
        musubi_v2_namespace="nyla/voice",
        musubi_v2_presence="nyla/voice",
    )

    await _unwrap(MemoryToolsMixin.musubi_search)(agent, query="prank", limit=5)

    assert len(stub.calls) == 1
    call = stub.calls[0]
    assert call["namespace"] == "nyla/*/episodic"


@pytest.mark.asyncio
async def test_musubi_search_passes_state_filter_for_fresh_save_recall(agent):
    """The whole point of musubi_search is recalling a deliberate
    musubi_remember BEFORE the maturation cron runs (otherwise voice-Nyla
    can't remember what Discord-Nyla just saved). Asserts state_filter
    explicitly includes `provisional` so fresh stores are visible."""
    stub = _StubClient(response={"results": []})
    agent._musubi_v2_client = lambda: stub
    agent.config = AgentConfig(
        agent_name="nyla",
        memory_agent_tag="nyla-voice",
        discord_room="channel:0",
        musubi_v2_namespace="nyla/voice",
        musubi_v2_presence="nyla/voice",
    )

    await _unwrap(MemoryToolsMixin.musubi_search)(agent, query="anything", limit=5)

    call = stub.calls[0]
    assert call["state_filter"] == ["provisional", "matured", "promoted"]
    # Mode "deep" — recall waits on full hybrid + rerank for best hit.
    assert call["mode"] == "deep"


@pytest.mark.asyncio
async def test_musubi_search_returns_origin_channel_in_each_row(agent):
    """Result rows must surface their concrete stored namespace's
    presence segment so the LLM can attribute "you told me on Discord"
    vs "on the call". Without this, channel provenance is lost in
    rendering even though the API preserves it."""
    stub = _StubClient(
        response={
            "results": [
                {
                    "object_id": "a" * 27,
                    "score": 0.9,
                    "plane": "episodic",
                    "content": "the cocoa-pods prank",
                    "namespace": "nyla/discord/episodic",
                },
            ],
        },
    )
    agent._musubi_v2_client = lambda: stub
    agent.config = AgentConfig(
        agent_name="nyla",
        memory_agent_tag="nyla-voice",
        discord_room="channel:0",
        musubi_v2_namespace="nyla/voice",
        musubi_v2_presence="nyla/voice",
    )

    rendered = await _unwrap(MemoryToolsMixin.musubi_search)(agent, query="prank")
    assert "[discord]" in rendered, rendered
    assert "cocoa-pods prank" in rendered, rendered


# ---------------------------------------------------------------------------
# musubi_think behaviour — namespace shape, presence resolution, ack
# ---------------------------------------------------------------------------


def test_memory_mixin_has_musubi_think() -> None:
    assert hasattr(MusubiToolsMixin, "musubi_think")
    assert callable(MusubiToolsMixin.musubi_think)


def test_memory_mixin_exposes_think_impl_helper() -> None:
    """``think_impl`` is the plain-async body so tests (and post-call
    hooks, if any) can invoke without the ``@function_tool`` descriptor."""
    assert hasattr(MusubiToolsMixin, "think_impl")
    assert callable(MusubiToolsMixin.think_impl)


class _ThoughtStub:
    """Records send_thought calls so tests can assert wire shape."""

    def __init__(self, ack: dict[str, Any] | None = None) -> None:
        self._ack = ack or {"object_id": "thought-" + "0" * 20, "state": "delivered"}
        self.calls: list[dict[str, Any]] = []

    async def send_thought(
        self,
        *,
        namespace: str,
        from_presence: str,
        to_presence: str,
        content: str,
        channel: str = "default",
        importance: int = 5,
        session: object | None = None,
    ) -> dict[str, Any]:
        self.calls.append(
            {
                "namespace": namespace,
                "from_presence": from_presence,
                "to_presence": to_presence,
                "content": content,
                "channel": channel,
                "importance": importance,
            }
        )
        return self._ack


@pytest.mark.asyncio
async def test_musubi_think_uses_own_thought_namespace(agent) -> None:
    """``musubi_think`` must send from ``<agent>/<channel>/thought`` —
    ADR 0030 agent-as-tenant form. Regression to legacy ``eric/<agent>``
    breaks scope-token validation on the live server."""
    stub = _ThoughtStub()
    agent._musubi_v2_client = lambda: stub
    agent.config = AgentConfig(
        agent_name="aoi",
        memory_agent_tag="aoi-voice",
        discord_room="channel:0",
        musubi_v2_namespace="aoi/voice",
        musubi_v2_presence="aoi/voice",
    )

    await _unwrap(MusubiToolsMixin.musubi_think)(agent, to_presence="nyla/voice", content="hey")

    assert len(stub.calls) == 1
    call = stub.calls[0]
    assert call["namespace"] == "aoi/voice/thought"
    assert call["from_presence"] == "aoi/voice"
    assert call["to_presence"] == "nyla/voice"


@pytest.mark.asyncio
async def test_musubi_think_resolves_bare_recipient_to_own_channel(agent) -> None:
    """A bare ``<agent>`` recipient must be resolved to ``<agent>/<own-channel>``
    so the model doesn't have to know channel topology to page a peer."""
    stub = _ThoughtStub()
    agent._musubi_v2_client = lambda: stub
    agent.config = AgentConfig(
        agent_name="aoi",
        memory_agent_tag="aoi-voice",
        discord_room="channel:0",
        musubi_v2_namespace="aoi/voice",
        musubi_v2_presence="aoi/voice",
    )

    await _unwrap(MusubiToolsMixin.musubi_think)(agent, to_presence="nyla", content="ping")

    assert stub.calls[0]["to_presence"] == "nyla/voice"


@pytest.mark.asyncio
async def test_musubi_think_rejects_empty_recipient_or_content(agent) -> None:
    """Validation lives in ``think_impl`` so an empty arg degrades to a
    user-readable error instead of a 400 from the server."""
    stub = _ThoughtStub()
    agent._musubi_v2_client = lambda: stub
    agent.config = AgentConfig(
        agent_name="aoi",
        memory_agent_tag="aoi-voice",
        discord_room="channel:0",
        musubi_v2_namespace="aoi/voice",
        musubi_v2_presence="aoi/voice",
    )

    empty_recipient = await _unwrap(MusubiToolsMixin.musubi_think)(
        agent, to_presence="", content="hi"
    )
    empty_content = await _unwrap(MusubiToolsMixin.musubi_think)(
        agent, to_presence="nyla", content=""
    )

    assert "to_presence is required" in empty_recipient
    assert "content is required" in empty_content
    assert stub.calls == []


@pytest.mark.asyncio
async def test_musubi_think_returns_object_id_in_ack(agent) -> None:
    """The ack rendering must surface the resolved recipient + the
    object_id so the LLM can confirm delivery in its reply."""
    stub = _ThoughtStub(ack={"object_id": "thought-abc123", "state": "delivered"})
    agent._musubi_v2_client = lambda: stub
    agent.config = AgentConfig(
        agent_name="aoi",
        memory_agent_tag="aoi-voice",
        discord_room="channel:0",
        musubi_v2_namespace="aoi/voice",
        musubi_v2_presence="aoi/voice",
    )

    rendered = await _unwrap(MusubiToolsMixin.musubi_think)(
        agent, to_presence="nyla/discord", content="deploy is done"
    )

    assert "nyla/discord" in rendered
    assert "thought-abc123" in rendered


# ---------------------------------------------------------------------------
# musubi_get — deferred stub
# ---------------------------------------------------------------------------


def test_memory_mixin_has_musubi_get() -> None:
    """``musubi_get`` must register so its name is reserved at the
    canonical surface even before the SDK extension lands."""
    assert hasattr(MusubiToolsMixin, "musubi_get")
    assert callable(MusubiToolsMixin.musubi_get)


@pytest.mark.asyncio
async def test_musubi_get_returns_deferred_message(agent) -> None:
    """Per Musubi ADR 0032 + CLAUDE.md "ADR-punted dependencies must
    fail loud" rule: until the SDK gains per-plane ``.get()`` accessors,
    ``musubi_get`` returns a clear deferred message rather than silently
    no-op'ing or claiming success."""
    rendered = await _unwrap(MusubiToolsMixin.musubi_get)(
        agent, plane="episodic", namespace="aoi/voice/episodic", object_id="x" * 27
    )

    assert "not yet available" in rendered.lower()
    # Must point the model back at musubi_search so it gets a graceful
    # fall-back path instead of giving up on the recall.
    assert "musubi_search" in rendered
