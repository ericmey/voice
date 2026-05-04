"""Tests for `sdk.musubi_v2_client` — the new-stack Musubi client.

Structural + behavioral. Behavioral tests monkeypatch `aiohttp.ClientSession`
via a tiny fake so no real HTTP flies, and so the tests run without a live
Musubi on `localhost:8100`.
"""

from __future__ import annotations

import asyncio
import json
from typing import Any

import pytest
from sdk.musubi_v2_client import (
    DEFAULT_BASE_URL,
    MUSUBI_V2_BASE_URL_ENV,
    MUSUBI_V2_TOKEN_ENV,
    MusubiV2AuthError,
    MusubiV2Client,
    MusubiV2ClientConfig,
    MusubiV2ClientError,
    MusubiV2ServerError,
    MusubiV2TimeoutError,
    capture_memory,
    list_episodic,
    retrieve,
    send_thought,
)

from sdk import musubi_v2_client

# ---------------------------------------------------------------------------
# Fake aiohttp session — records calls, returns scripted responses.
# ---------------------------------------------------------------------------


class _FakeResponse:
    """Minimal aiohttp.ClientResponse substitute for the paths we use."""

    def __init__(self, status: int, body: Any) -> None:
        self.status = status
        self._body = body

    async def text(self) -> str:
        if isinstance(self._body, str):
            return self._body
        return json.dumps(self._body)

    async def json(self, content_type: str | None = None) -> Any:
        if isinstance(self._body, str):
            return json.loads(self._body)
        return self._body

    async def __aenter__(self) -> _FakeResponse:
        return self

    async def __aexit__(self, *exc: object) -> None:
        return None


class _FakeSession:
    def __init__(self, responses: list[_FakeResponse | Exception]) -> None:
        self._responses = responses
        self.calls: list[dict[str, Any]] = []

    def post(self, url: str, *, json: dict[str, Any], headers: dict[str, str]) -> Any:
        self.calls.append({"method": "POST", "url": url, "json": json, "headers": dict(headers)})
        return self._next_response()

    def get(
        self, url: str, *, params: dict[str, str] | None = None, headers: dict[str, str]
    ) -> Any:
        self.calls.append(
            {"method": "GET", "url": url, "params": dict(params or {}), "headers": dict(headers)}
        )
        return self._next_response()

    def _next_response(self) -> Any:
        if not self._responses:
            raise AssertionError("no scripted response")
        resp = self._responses.pop(0)
        if isinstance(resp, Exception):
            return _RaisingCtx(resp)
        return resp

    async def __aenter__(self) -> _FakeSession:
        return self

    async def __aexit__(self, *exc: object) -> None:
        return None


class _RaisingCtx:
    def __init__(self, exc: Exception) -> None:
        self._exc = exc

    async def __aenter__(self) -> None:
        raise self._exc

    async def __aexit__(self, *exc: object) -> None:
        return None


def _cfg(token: str = "test-token") -> MusubiV2ClientConfig:
    return MusubiV2ClientConfig(base_url="http://musubi.test/v1", token=token, timeout_s=1.0)


def _run(coro: Any) -> Any:
    return asyncio.get_event_loop().run_until_complete(coro)


# ---------------------------------------------------------------------------
# Config + env resolution
# ---------------------------------------------------------------------------


def test_default_base_url_exported() -> None:
    assert DEFAULT_BASE_URL.startswith("http")


def test_from_env_reads_both_vars(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(MUSUBI_V2_BASE_URL_ENV, "https://musubi.example.com/v1/")
    monkeypatch.setenv(MUSUBI_V2_TOKEN_ENV, "real-token")
    cfg = MusubiV2ClientConfig.from_env()
    # Trailing slash stripped.
    assert cfg.base_url == "https://musubi.example.com/v1"
    assert cfg.token == "real-token"


def test_from_env_applies_defaults_when_missing(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv(MUSUBI_V2_BASE_URL_ENV, raising=False)
    monkeypatch.delenv(MUSUBI_V2_TOKEN_ENV, raising=False)
    cfg = MusubiV2ClientConfig.from_env()
    assert cfg.base_url == DEFAULT_BASE_URL.rstrip("/")
    assert cfg.token == ""


@pytest.mark.asyncio
async def test_shared_session_reuses_keepalive_session(monkeypatch: pytest.MonkeyPatch) -> None:
    class FakeConnector:
        def __init__(self, **kwargs) -> None:
            self.kwargs = kwargs

    class FakeClientSession:
        created = 0

        def __init__(self, **kwargs) -> None:
            FakeClientSession.created += 1
            self.kwargs = kwargs
            self.closed = False

    monkeypatch.setattr(musubi_v2_client.aiohttp, "TCPConnector", FakeConnector)
    monkeypatch.setattr(musubi_v2_client.aiohttp, "ClientSession", FakeClientSession)
    musubi_v2_client._shared_sessions.clear()

    first = musubi_v2_client._shared_session_for(2.0)
    second = musubi_v2_client._shared_session_for(2.0)

    assert first is second
    assert FakeClientSession.created == 1
    # ``_shared_session_for`` is typed as returning ``aiohttp.ClientSession``;
    # the monkeypatch above swaps in ``FakeClientSession``. Narrow with
    # isinstance so pyright sees the fake's ``.kwargs`` attribute.
    assert isinstance(first, FakeClientSession)
    assert first.kwargs["connector"].kwargs["limit"] == 20


# ---------------------------------------------------------------------------
# capture_memory — request shape, headers, error translation
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_capture_memory_posts_expected_shape() -> None:
    session = _FakeSession([_FakeResponse(200, {"object_id": "m" * 27, "state": "provisional"})])
    ack = await capture_memory(
        _cfg(),
        namespace="eric/test/episodic",
        content="remember this",
        tags=["livekit", "demo"],
        importance=7,
        idempotency_key="fixed-key",
        session=session,  # type: ignore[arg-type]
    )
    assert ack["object_id"] == "m" * 27
    assert len(session.calls) == 1
    call = session.calls[0]
    assert call["url"] == "http://musubi.test/v1/episodic"
    # Canonical `CaptureRequest` accepts namespace/content/tags/
    # importance (+ optional summary/created_at). `topics` used to
    # be sent and silently dropped server-side; callers now fold
    # topics into `tags` at the call site.
    assert call["json"] == {
        "namespace": "eric/test/episodic",
        "content": "remember this",
        "tags": ["livekit", "demo"],
        "importance": 7,
    }
    # Bearer + request-id + idempotency.
    assert call["headers"]["Authorization"] == "Bearer test-token"
    assert call["headers"]["Idempotency-Key"] == "fixed-key"
    assert "X-Request-Id" in call["headers"]


@pytest.mark.asyncio
async def test_capture_memory_generates_idempotency_when_absent() -> None:
    session = _FakeSession([_FakeResponse(200, {"object_id": "m" * 27})])
    await capture_memory(
        _cfg(),
        namespace="ns",
        content="c",
        session=session,  # type: ignore[arg-type]
    )
    idem = session.calls[0]["headers"]["Idempotency-Key"]
    assert idem
    # 32-hex UUID shape (no dashes) per `uuid.uuid4().hex`.
    assert len(idem) == 32 and all(c in "0123456789abcdef" for c in idem)


@pytest.mark.asyncio
async def test_401_raises_auth_error() -> None:
    session = _FakeSession([_FakeResponse(401, {"detail": "bad token"})])
    with pytest.raises(MusubiV2AuthError):
        await capture_memory(
            _cfg(),
            namespace="ns",
            content="c",
            session=session,  # type: ignore[arg-type]
        )


@pytest.mark.asyncio
async def test_403_raises_auth_error() -> None:
    session = _FakeSession([_FakeResponse(403, {"detail": "out of scope"})])
    with pytest.raises(MusubiV2AuthError):
        await capture_memory(
            _cfg(),
            namespace="ns",
            content="c",
            session=session,  # type: ignore[arg-type]
        )


@pytest.mark.asyncio
async def test_4xx_other_raises_client_error() -> None:
    session = _FakeSession([_FakeResponse(422, {"detail": "invalid"})])
    with pytest.raises(MusubiV2ClientError):
        await capture_memory(
            _cfg(),
            namespace="ns",
            content="c",
            session=session,  # type: ignore[arg-type]
        )


@pytest.mark.asyncio
async def test_5xx_raises_server_error() -> None:
    session = _FakeSession([_FakeResponse(503, "boom")])
    with pytest.raises(MusubiV2ServerError):
        await capture_memory(
            _cfg(),
            namespace="ns",
            content="c",
            session=session,  # type: ignore[arg-type]
        )


@pytest.mark.asyncio
async def test_timeout_raises_timeout_error() -> None:
    session = _FakeSession([TimeoutError("no response")])
    with pytest.raises(MusubiV2TimeoutError):
        await capture_memory(
            _cfg(),
            namespace="ns",
            content="c",
            session=session,  # type: ignore[arg-type]
        )


# ---------------------------------------------------------------------------
# retrieve
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_retrieve_posts_expected_shape() -> None:
    session = _FakeSession([_FakeResponse(200, {"results": [{"object_id": "r" * 27}]})])
    out = await retrieve(
        _cfg(),
        namespace="eric/test/episodic",
        query_text="find me",
        mode="deep",
        limit=3,
        session=session,  # type: ignore[arg-type]
    )
    assert out["results"][0]["object_id"] == "r" * 27
    call = session.calls[0]
    assert call["url"] == "http://musubi.test/v1/retrieve"
    assert call["json"] == {
        "namespace": "eric/test/episodic",
        "query_text": "find me",
        "mode": "deep",
        "limit": 3,
    }
    # retrieve is not idempotent — no Idempotency-Key header needed.
    assert "Idempotency-Key" not in call["headers"]


@pytest.mark.asyncio
async def test_retrieve_omits_optional_fields_when_none() -> None:
    """`planes`, `include_archived`, `state_filter` default to omitted —
    the wire shape stays minimal so a v1.0/v1.1 server (or any caller
    that doesn't need the v1.2 features) sees the same body it always
    has. Locks against an accidental `body["state_filter"] = None` that
    would change the keyset."""
    session = _FakeSession([_FakeResponse(200, {"results": []})])
    await retrieve(
        _cfg(),
        namespace="eric/test/episodic",
        query_text="x",
        session=session,  # type: ignore[arg-type]
    )
    body = session.calls[0]["json"]
    assert "planes" not in body
    assert "include_archived" not in body
    assert "state_filter" not in body


@pytest.mark.asyncio
async def test_retrieve_passes_state_filter_when_set() -> None:
    """Explicit `state_filter` for fresh-save recall (Musubi v1.2.0).
    Asserts the value reaches the wire — without this, phone Nyla
    silently misses provisional rows even after the API extension lands."""
    session = _FakeSession([_FakeResponse(200, {"results": []})])
    await retrieve(
        _cfg(),
        namespace="nyla/*/episodic",
        query_text="prank",
        state_filter=["provisional", "matured", "promoted"],
        session=session,  # type: ignore[arg-type]
    )
    body = session.calls[0]["json"]
    assert body["state_filter"] == ["provisional", "matured", "promoted"]


@pytest.mark.asyncio
async def test_retrieve_passes_planes_and_include_archived_when_set() -> None:
    session = _FakeSession([_FakeResponse(200, {"results": []})])
    await retrieve(
        _cfg(),
        namespace="nyla/voice",
        query_text="x",
        planes=["episodic", "curated"],
        include_archived=True,
        session=session,  # type: ignore[arg-type]
    )
    body = session.calls[0]["json"]
    assert body["planes"] == ["episodic", "curated"]
    assert body["include_archived"] is True


# ---------------------------------------------------------------------------
# send_thought
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_send_thought_posts_expected_shape() -> None:
    session = _FakeSession([_FakeResponse(200, {"object_id": "t" * 27})])
    ack = await send_thought(
        _cfg(),
        namespace="eric/test/thought",
        from_presence="eric/aoi",
        to_presence="eric/nyla",
        content="deploy is done",
        channel="scheduler",
        importance=6,
        session=session,  # type: ignore[arg-type]
    )
    assert ack["object_id"] == "t" * 27
    call = session.calls[0]
    assert call["url"] == "http://musubi.test/v1/thoughts/send"
    assert call["json"] == {
        "namespace": "eric/test/thought",
        "from_presence": "eric/aoi",
        "to_presence": "eric/nyla",
        "content": "deploy is done",
        "channel": "scheduler",
        "importance": 6,
    }
    # send_thought sets an idempotency key — dedup on retries.
    assert call["headers"]["Idempotency-Key"]


# ---------------------------------------------------------------------------
# list_episodic — GET shape + params + error translation
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_list_episodic_gets_expected_shape() -> None:
    session = _FakeSession(
        [_FakeResponse(200, {"items": [{"object_id": "e" * 27}], "next_cursor": "c1"})]
    )
    out = await list_episodic(
        _cfg(),
        namespace="eric/nyla/episodic",
        limit=50,
        session=session,  # type: ignore[arg-type]
    )
    assert out["items"][0]["object_id"] == "e" * 27
    assert out["next_cursor"] == "c1"
    call = session.calls[0]
    assert call["method"] == "GET"
    assert call["url"] == "http://musubi.test/v1/episodic"
    assert call["params"]["namespace"] == "eric/nyla/episodic"
    assert call["params"]["limit"] == "50"
    assert "cursor" not in call["params"]


@pytest.mark.asyncio
async def test_list_episodic_passes_cursor_when_set() -> None:
    session = _FakeSession([_FakeResponse(200, {"items": [], "next_cursor": None})])
    await list_episodic(
        _cfg(),
        namespace="eric/nyla/episodic",
        limit=10,
        cursor="abc123",
        session=session,  # type: ignore[arg-type]
    )
    assert session.calls[0]["params"]["cursor"] == "abc123"


@pytest.mark.asyncio
async def test_get_401_raises_auth_error() -> None:
    session = _FakeSession([_FakeResponse(401, {"detail": "bad token"})])
    with pytest.raises(MusubiV2AuthError):
        await list_episodic(
            _cfg(),
            namespace="ns",
            session=session,  # type: ignore[arg-type]
        )


@pytest.mark.asyncio
async def test_get_403_raises_auth_error() -> None:
    session = _FakeSession([_FakeResponse(403, {"detail": "out of scope"})])
    with pytest.raises(MusubiV2AuthError):
        await list_episodic(
            _cfg(),
            namespace="ns",
            session=session,  # type: ignore[arg-type]
        )


@pytest.mark.asyncio
async def test_get_4xx_raises_client_error() -> None:
    session = _FakeSession([_FakeResponse(422, {"detail": "invalid namespace"})])
    with pytest.raises(MusubiV2ClientError):
        await list_episodic(
            _cfg(),
            namespace="ns",
            session=session,  # type: ignore[arg-type]
        )


@pytest.mark.asyncio
async def test_get_5xx_raises_server_error() -> None:
    session = _FakeSession([_FakeResponse(503, "boom")])
    with pytest.raises(MusubiV2ServerError):
        await list_episodic(
            _cfg(),
            namespace="ns",
            session=session,  # type: ignore[arg-type]
        )


@pytest.mark.asyncio
async def test_get_timeout_raises_timeout_error() -> None:
    session = _FakeSession([TimeoutError("no response")])
    with pytest.raises(MusubiV2TimeoutError):
        await list_episodic(
            _cfg(),
            namespace="ns",
            session=session,  # type: ignore[arg-type]
        )


# ---------------------------------------------------------------------------
# Facade class
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_musubi_v2_client_facade_proxies_each_action() -> None:
    session = _FakeSession(
        [
            _FakeResponse(200, {"object_id": "m" * 27}),
            _FakeResponse(200, {"results": []}),
            _FakeResponse(200, {"object_id": "t" * 27}),
            _FakeResponse(200, {"items": []}),
        ]
    )
    client = MusubiV2Client(config=_cfg())
    ack1 = await client.capture_memory(
        namespace="n",
        content="c",
        session=session,  # type: ignore[arg-type]
    )
    ack2 = await client.retrieve(
        namespace="n",
        query_text="q",
        session=session,  # type: ignore[arg-type]
    )
    ack3 = await client.send_thought(
        namespace="n",
        from_presence="eric/aoi",
        to_presence="eric/nyla",
        content="hi",
        session=session,  # type: ignore[arg-type]
    )
    ack4 = await client.list_episodic(
        namespace="n",
        session=session,  # type: ignore[arg-type]
    )
    assert ack1["object_id"] == "m" * 27
    assert ack2 == {"results": []}
    assert ack3["object_id"] == "t" * 27
    assert ack4 == {"items": []}
    assert len(session.calls) == 4
