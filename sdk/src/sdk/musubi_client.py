"""Qdrant / Musubi client — direct REST for musubi_recent and memory_store."""

from __future__ import annotations

import os
from typing import Any

import aiohttp

# Qdrant is reached directly over localhost:6333 — no MCP, no subprocess,
# no Python client library.
MUSUBI_COLLECTION = "musubi_memories"
MUSUBI_TIMEOUT_S = 0.5  # hard 500ms budget — same as vcr's musubi-client.ts

# Shared TCP connector for embedding calls. Sessions are scoped to
# individual requests, so there is no module-level ClientSession to close.
_embed_connector: aiohttp.TCPConnector | None = None


def _shared_embed_connector() -> aiohttp.TCPConnector:
    global _embed_connector
    if _embed_connector is None or _embed_connector.closed:
        _embed_connector = aiohttp.TCPConnector(limit=5)
    return _embed_connector


# Connector cleanup: each per-request ClientSession owns and closes the
# connector it used. The module-level reference is replaced on the next
# call if that connector is closed.


def qdrant_url() -> str:
    """Resolve Qdrant URL from environment at call time, not import time."""
    host = os.environ.get("QDRANT_HOST", "localhost")
    port = os.environ.get("QDRANT_PORT", "6333")
    return f"http://{host}:{port}"


# Gemini embedding config for memory_store — matches musubi's embedding.py
GEMINI_EMBED_URL = (
    "https://generativelanguage.googleapis.com/v1beta/models/gemini-embedding-001:embedContent"
)


def _gemini_api_key() -> str:
    return os.environ.get("GEMINI_API_KEY") or os.environ.get("GOOGLE_API_KEY") or ""


async def async_embed_text(
    text: str, *, session: aiohttp.ClientSession | None = None
) -> list[float]:
    """Get a Gemini embedding vector for *text* via async HTTP.

    Uses the REST API directly to stay on the async event loop — no sync
    ``google.genai`` import, no thread executor. Matches the musubi library's
    model (gemini-embedding-001) and vector size (3072).

    Security note: the Gemini REST API requires the key in the URL query
    string (``?key=...``). This is a Google-side design decision; the key
    travels in the query param and may appear in proxy logs. We minimize
    exposure by never logging the assembled URL.
    """
    api_key = _gemini_api_key()
    if not api_key:
        raise RuntimeError("No GEMINI_API_KEY or GOOGLE_API_KEY in environment")
    # Google requires the key as a query parameter; see security note above.
    url = f"{GEMINI_EMBED_URL}?key={api_key}"
    body: dict[str, Any] = {
        "model": "models/gemini-embedding-001",
        "content": {"parts": [{"text": text}]},
    }

    async def _do_post(http: aiohttp.ClientSession) -> list[float]:
        async with http.post(url, json=body) as resp:
            if resp.status != 200:
                err_text = (await resp.text())[:200]
                raise RuntimeError(f"Gemini embedding API {resp.status}: {err_text}")
            data = await resp.json()
        values = data.get("embedding", {}).get("values")
        if not values:
            raise RuntimeError("Gemini returned no embedding values")
        return values

    if session is not None:
        return await _do_post(session)

    timeout = aiohttp.ClientTimeout(total=5)
    async with aiohttp.ClientSession(
        connector=_shared_embed_connector(),
        timeout=timeout,
    ) as http:
        return await _do_post(http)
