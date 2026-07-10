"""Musubi client — async HTTP client for the canonical Musubi API.

Talks to Musubi's HTTP/JSON API at ``/v1/*`` with bearer auth. This is the
*only* Musubi client in the repo; the earlier v1 client was deleted. The
``MUSUBI_V2_*`` env and namespace strings are kept for deploy-config
compatibility (the live host and compose supply them) — the "v2" now lives
only in those strings, not in the code surface, which is plain ``MusubiClient``.

Scope: just enough surface for the canonical agent-tools mixin
(``musubi_search`` / ``musubi_recent`` / ``musubi_remember``, plus the
retained ``think_impl`` send). The full Musubi SDK lives upstream in the
Musubi monorepo under ``src/musubi/sdk/``; a thin local client here is
cheaper than taking that dependency until a second consumer needs it.

No embedding is done client-side. The API embeds server-side (TEI + BGE-M3),
so the client's job is to shape requests, handle auth, and translate errors
into typed exceptions the tool layer can reason about.

Transport: aiohttp, matching the repo's async HTTP posture. Per-call timeout
defaults to ``MUSUBI_V2_TIMEOUT_S`` (2s) — a voice tool can't wait longer and
still feel responsive; the tool layer catches the timeout and degrades.
"""

from __future__ import annotations

import asyncio
import logging
import os
import uuid
from dataclasses import dataclass
from typing import Any

import aiohttp

logger = logging.getLogger("voice.musubi-v2")

# Environment-driven so the same client runs against local/dev/prod Musubi
# instances without code changes. The ``MUSUBI_V2_*`` env names are historical
# (kept for deploy-config compatibility) — there is no other Musubi client to
# collide with anymore.
MUSUBI_V2_BASE_URL_ENV = "MUSUBI_V2_BASE_URL"
MUSUBI_V2_TOKEN_ENV = "MUSUBI_V2_TOKEN"
DEFAULT_BASE_URL = "http://localhost:8100/v1"

# Voice-facing tools live on a tight latency budget — 2s is already
# perceptible. A
# real Musubi with TEI reranking needs more, but still must feel
# responsive. Tools catch the timeout and degrade gracefully.
MUSUBI_V2_TIMEOUT_S = 2.0

_BEARER_HEADER = "Authorization"
_REQUEST_ID_HEADER = "X-Request-Id"
_IDEMPOTENCY_HEADER = "Idempotency-Key"
_shared_sessions: dict[tuple[int, float], aiohttp.ClientSession] = {}


def _shared_session_for(timeout_s: float) -> aiohttp.ClientSession:
    """Return an event-loop-local shared session for Musubi keep-alive."""
    loop = asyncio.get_running_loop()
    key = (id(loop), timeout_s)
    session = _shared_sessions.get(key)
    if session is None or session.closed:
        timeout = aiohttp.ClientTimeout(total=timeout_s)
        connector = aiohttp.TCPConnector(limit=20, keepalive_timeout=120)
        session = aiohttp.ClientSession(connector=connector, timeout=timeout)
        _shared_sessions[key] = session
    return session


async def close_shared_sessions() -> None:
    """Close all event-loop-local shared HTTP sessions."""
    sessions = list(_shared_sessions.values())
    _shared_sessions.clear()
    for session in sessions:
        if session.closed:
            continue
        try:
            await session.close()
        except Exception as exc:
            logger.warning("Musubi v2 session close failed: %s", exc)


def wire_musubi_shutdown(ctx: Any) -> None:
    """Register a LiveKit job shutdown hook for Musubi v2 HTTP sessions."""
    add_shutdown_callback = getattr(ctx, "add_shutdown_callback", None)
    if add_shutdown_callback is None:
        return

    async def _close_musubi_v2(*_args: Any) -> None:
        await close_shared_sessions()

    try:
        add_shutdown_callback(_close_musubi_v2)
    except Exception as exc:
        logger.warning("Musubi v2 shutdown hook registration failed: %s", exc)


@dataclass(frozen=True)
class MusubiClientConfig:
    """Client-level config. Resolved from env by default; tests pass
    explicit values to avoid env leakage."""

    base_url: str
    token: str
    timeout_s: float = MUSUBI_V2_TIMEOUT_S

    @classmethod
    def from_env(cls, *, timeout_s: float | None = None) -> MusubiClientConfig:
        base_url = os.environ.get(MUSUBI_V2_BASE_URL_ENV, DEFAULT_BASE_URL).rstrip("/")
        token = os.environ.get(MUSUBI_V2_TOKEN_ENV, "")
        return cls(
            base_url=base_url,
            token=token,
            timeout_s=timeout_s if timeout_s is not None else MUSUBI_V2_TIMEOUT_S,
        )


class MusubiError(Exception):
    """Parent for every error the v2 client raises — tool layer catches
    this to present a single degraded-mode message to the voice."""


class MusubiAuthError(MusubiError):
    """401/403 from Musubi. Token is missing, expired, or out of scope.
    Distinct so the tool layer can log at ERROR (not WARN) — auth
    failures are not transient."""


class MusubiTimeoutError(MusubiError):
    """Request didn't complete inside `timeout_s`. Voice tools degrade
    to a "couldn't check memory" response without blocking the call."""


class MusubiServerError(MusubiError):
    """5xx from Musubi. Transient — the next tool call might succeed."""


class MusubiClientError(MusubiError):
    """4xx other than auth — caller gave us a bad payload. Bug, not
    runtime transient. Log loudly."""


async def capture_memory(
    config: MusubiClientConfig,
    *,
    namespace: str,
    content: str,
    tags: list[str] | None = None,
    importance: int = 5,
    idempotency_key: str | None = None,
    session: aiohttp.ClientSession | None = None,
) -> dict[str, Any]:
    """POST /v1/episodic — capture an episodic memory.

    Returns the ack dict (includes `object_id` + lifecycle state). The
    caller owns `idempotency_key`; absent one, we generate a fresh UUID
    so a retried tool call doesn't double-post.

    Canonical `CaptureRequest` accepts `{namespace, content, summary?,
    tags, importance, created_at?}` — anything else is dropped
    server-side by pydantic `extra="ignore"`. This client used to
    also send `topics`; drop it here rather than silently lose data.
    Callers that want both should fold topics into `tags` at the call
    site.
    """
    body: dict[str, Any] = {
        "namespace": namespace,
        "content": content,
        "tags": tags or [],
        "importance": importance,
    }
    return await _post(
        config,
        path="/episodic",
        body=body,
        idempotency_key=idempotency_key or uuid.uuid4().hex,
        session=session,
    )


async def retrieve(
    config: MusubiClientConfig,
    *,
    namespace: str,
    query_text: str,
    mode: str = "fast",
    limit: int = 10,
    planes: list[str] | None = None,
    include_archived: bool = False,
    state_filter: list[str] | None = None,
    session: aiohttp.ClientSession | None = None,
) -> dict[str, Any]:
    """POST /v1/retrieve — hybrid retrieve across planes.

    `mode` is "fast" (dense-cache path) or "deep" (dense + sparse +
    rerank). Voice tools default to "deep" for recall (the user waited
    to ask; give them the best hit) but "fast" is available for
    latency-sensitive supplements.

    `namespace` accepts the canonical shapes (Musubi ADRs 0028 + 0031):
    3-segment concrete (`<tenant>/<presence>/<plane>`), 2-segment
    cross-plane (`<tenant>/<presence>` + `planes`), or wildcard with
    `*` replacing any segment (e.g. `nyla/*/episodic` for cross-channel
    recall within a tenant). Wildcards are read-only — captures still
    write to the channel-tagged 3-segment slot.
    """
    body: dict[str, Any] = {
        "namespace": namespace,
        "query_text": query_text,
        "mode": mode,
        "limit": limit,
    }
    if planes is not None:
        body["planes"] = planes
    if include_archived:
        body["include_archived"] = True
    if state_filter is not None:
        body["state_filter"] = state_filter
    return await _post(config, path="/retrieve", body=body, session=session)


async def send_thought(
    config: MusubiClientConfig,
    *,
    namespace: str,
    from_presence: str,
    to_presence: str,
    content: str,
    channel: str = "default",
    importance: int = 5,
    session: aiohttp.ClientSession | None = None,
) -> dict[str, Any]:
    """POST /v1/thoughts/send — deliver a thought presence-to-presence.

    Recipients see it live via the SSE stream (`/v1/thoughts/stream`);
    inbox scroll is via `/v1/thoughts/check`.
    """
    body = {
        "namespace": namespace,
        "from_presence": from_presence,
        "to_presence": to_presence,
        "content": content,
        "channel": channel,
        "importance": importance,
    }
    return await _post(
        config, path="/thoughts/send", body=body, idempotency_key=uuid.uuid4().hex, session=session
    )


async def list_episodic(
    config: MusubiClientConfig,
    *,
    namespace: str,
    limit: int = 50,
    cursor: str | None = None,
    session: aiohttp.ClientSession | None = None,
) -> dict[str, Any]:
    """GET /v1/episodic — list memories in a namespace.

    Returns ``{"items": [...], "next_cursor": str|null}``. Items are
    whatever order the server's scroll returns; callers that want
    time-descending should sort by ``created_epoch`` client-side.

    Used by ``MusubiToolsMixin.fetch_recent_context``.
    """
    params = {"namespace": namespace, "limit": str(limit)}
    if cursor:
        params["cursor"] = cursor
    return await _get(config, path="/episodic", params=params, session=session)


async def _get(
    config: MusubiClientConfig,
    *,
    path: str,
    params: dict[str, str] | None = None,
    session: aiohttp.ClientSession | None = None,
) -> dict[str, Any]:
    """Shared GET path — mirrors ``_post`` for read endpoints."""
    url = f"{config.base_url}{path}"
    headers: dict[str, str] = {
        _BEARER_HEADER: f"Bearer {config.token}",
        _REQUEST_ID_HEADER: uuid.uuid4().hex,
    }

    async def _do(http: aiohttp.ClientSession) -> dict[str, Any]:
        try:
            async with http.get(url, params=params, headers=headers) as resp:
                text = await resp.text()
                if resp.status == 401 or resp.status == 403:
                    raise MusubiAuthError(f"{resp.status} on {path}: {text[:200]}")
                if 400 <= resp.status < 500:
                    raise MusubiClientError(f"{resp.status} on {path}: {text[:200]}")
                if resp.status >= 500:
                    raise MusubiServerError(f"{resp.status} on {path}: {text[:200]}")
                if not text:
                    return {}
                try:
                    data = await resp.json(content_type=None)
                except Exception as exc:
                    raise MusubiServerError(f"non-JSON response on {path}: {exc}") from exc
                if not isinstance(data, dict):
                    raise MusubiServerError(f"expected JSON object on {path}, got {type(data)!r}")
                return data
        except TimeoutError as exc:
            raise MusubiTimeoutError(f"timeout on {path} after {config.timeout_s}s") from exc

    if session is not None:
        return await _do(session)

    return await _do(_shared_session_for(config.timeout_s))


async def _post(
    config: MusubiClientConfig,
    *,
    path: str,
    body: dict[str, Any],
    idempotency_key: str | None = None,
    session: aiohttp.ClientSession | None = None,
) -> dict[str, Any]:
    """Shared POST path — one place for headers, timeout, and error
    translation. Everything here is structural; no business logic."""
    url = f"{config.base_url}{path}"
    headers: dict[str, str] = {
        _BEARER_HEADER: f"Bearer {config.token}",
        _REQUEST_ID_HEADER: uuid.uuid4().hex,
    }
    if idempotency_key is not None:
        headers[_IDEMPOTENCY_HEADER] = idempotency_key

    async def _do(http: aiohttp.ClientSession) -> dict[str, Any]:
        try:
            async with http.post(url, json=body, headers=headers) as resp:
                text = await resp.text()
                if resp.status == 401 or resp.status == 403:
                    raise MusubiAuthError(f"{resp.status} on {path}: {text[:200]}")
                if 400 <= resp.status < 500:
                    raise MusubiClientError(f"{resp.status} on {path}: {text[:200]}")
                if resp.status >= 500:
                    raise MusubiServerError(f"{resp.status} on {path}: {text[:200]}")
                if not text:
                    return {}
                try:
                    data = await resp.json(content_type=None)
                except Exception as exc:
                    raise MusubiServerError(f"non-JSON response on {path}: {exc}") from exc
                if not isinstance(data, dict):
                    raise MusubiServerError(f"expected JSON object on {path}, got {type(data)!r}")
                return data
        except TimeoutError as exc:
            raise MusubiTimeoutError(f"timeout on {path} after {config.timeout_s}s") from exc

    if session is not None:
        return await _do(session)

    return await _do(_shared_session_for(config.timeout_s))


@dataclass(frozen=True)
class MusubiClient:
    """Small convenience facade — binds a config + optional shared
    session and offers the three canonical actions as methods so the
    tool layer can spin up one client per mixin and reuse it.

    The functional form (`capture_memory(config, ...)`, etc.) is the
    primary API; this class is just ergonomic sugar for call sites
    that don't want to thread `config` through every call.
    """

    config: MusubiClientConfig

    async def capture_memory(
        self,
        *,
        namespace: str,
        content: str,
        tags: list[str] | None = None,
        importance: int = 5,
        idempotency_key: str | None = None,
        session: aiohttp.ClientSession | None = None,
    ) -> dict[str, Any]:
        return await capture_memory(
            self.config,
            namespace=namespace,
            content=content,
            tags=tags,
            importance=importance,
            idempotency_key=idempotency_key,
            session=session,
        )

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
        session: aiohttp.ClientSession | None = None,
    ) -> dict[str, Any]:
        return await retrieve(
            self.config,
            namespace=namespace,
            query_text=query_text,
            mode=mode,
            limit=limit,
            planes=planes,
            include_archived=include_archived,
            state_filter=state_filter,
            session=session,
        )

    async def send_thought(
        self,
        *,
        namespace: str,
        from_presence: str,
        to_presence: str,
        content: str,
        channel: str = "default",
        importance: int = 5,
        session: aiohttp.ClientSession | None = None,
    ) -> dict[str, Any]:
        return await send_thought(
            self.config,
            namespace=namespace,
            from_presence=from_presence,
            to_presence=to_presence,
            content=content,
            channel=channel,
            importance=importance,
            session=session,
        )

    async def list_episodic(
        self,
        *,
        namespace: str,
        limit: int = 50,
        cursor: str | None = None,
        session: aiohttp.ClientSession | None = None,
    ) -> dict[str, Any]:
        return await list_episodic(
            self.config,
            namespace=namespace,
            limit=limit,
            cursor=cursor,
            session=session,
        )


__all__ = [
    "DEFAULT_BASE_URL",
    "MUSUBI_V2_BASE_URL_ENV",
    "MUSUBI_V2_TIMEOUT_S",
    "MUSUBI_V2_TOKEN_ENV",
    "MusubiAuthError",
    "MusubiClient",
    "MusubiClientConfig",
    "MusubiClientError",
    "MusubiError",
    "MusubiServerError",
    "MusubiTimeoutError",
    "capture_memory",
    "close_shared_sessions",
    "list_episodic",
    "retrieve",
    "send_thought",
    "wire_musubi_shutdown",
]
