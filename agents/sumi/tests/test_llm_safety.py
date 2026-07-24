"""Slice-4 SAFETY regression (v2, post second-read) — real path, real HTTP boundary.

Addresses the review blockers:

  F1 — exercises the ACTUAL worker construction via ``agent.build_llm()`` (not a
       test-local cap), and red-proves the env override is LOWER-ONLY: a value above
       the ceiling, non-numeric, or zero all FAIL LOUD; a valid lower value is
       honored. The outbound cap is asserted on the real *serialized HTTP request
       body* that the openai client sends.
  F2 — a real ``openai.AsyncClient`` over an ``httpx.MockTransport`` returning a
       custom ``httpx.AsyncByteStream``. The disconnect test asserts the TRANSPORT
       stream's ``aclose()`` is called (actual downstream HTTP closure), not a
       handwritten context manager. Still zero network.

Honest limitation: this proves the worker/OpenAI-client closes the downstream HTTP
stream on interrupt. Whether the LiteLLM proxy then aborts the momo upstream on that
close is proxy behavior NOT exercised here; the 64-token cap bounds the blast radius
regardless.
"""

from __future__ import annotations

import asyncio
import json

import httpx
import openai
import pytest
from agent import _LLM_MAX_TOKENS_CEILING, build_llm
from livekit.agents import llm as llm_mod

CEIL = _LLM_MAX_TOKENS_CEILING  # 64


def _sse(delta: str | None = None, finish: str | None = None, usage: bool = False) -> bytes:
    chunk = {
        "id": "chatcmpl-x",
        "object": "chat.completion.chunk",
        "created": 0,
        "model": "sumi",
        "choices": [
            {
                "index": 0,
                "delta": ({"role": "assistant", "content": delta} if delta is not None else {}),
                "finish_reason": finish,
            }
        ],
    }
    if usage:
        chunk["usage"] = {"prompt_tokens": 5, "completion_tokens": 3, "total_tokens": 8}
    return b"data: " + json.dumps(chunk).encode() + b"\n\n"


_DONE = b"data: [DONE]\n\n"


class _TrackingStream(httpx.AsyncByteStream):
    """httpx byte stream that records aclose() — the real downstream HTTP teardown.

    With endless=True it blocks after its parts so a mid-stream interrupt can be
    observed closing the transport stream.
    """

    def __init__(self, parts, *, endless: bool = False) -> None:
        self._parts = list(parts)
        self._endless = endless
        self.aclosed = False

    async def __aiter__(self):
        for part in self._parts:
            yield part
        if self._endless:
            await asyncio.sleep(3600)  # hold the connection open until interrupt

    async def aclose(self) -> None:
        self.aclosed = True


def _mock_client(parts, *, endless: bool = False, recorded: dict | None = None):
    """A real openai.AsyncClient whose HTTP layer is an httpx MockTransport.

    Records the serialized request body (for the outbound-cap assertion) and returns
    a streaming response backed by a _TrackingStream (for the aclose assertion).
    """
    stream = _TrackingStream(parts, endless=endless)

    def handler(request: httpx.Request) -> httpx.Response:
        if recorded is not None:
            recorded["body"] = json.loads(request.content)
        return httpx.Response(200, headers={"content-type": "text/event-stream"}, stream=stream)

    http = httpx.AsyncClient(transport=httpx.MockTransport(handler), base_url="http://mock.local/v1")
    oai = openai.AsyncClient(api_key="test-key", base_url="http://mock.local/v1", http_client=http)
    return oai, stream


def _ctx() -> llm_mod.ChatContext:
    c = llm_mod.ChatContext.empty()
    c.add_message(role="system", content="You are Sumi.")
    c.add_message(role="user", content="Hi.")
    return c


async def _drive_full_turn(recorded: dict) -> None:
    oai, _ = _mock_client([_sse("Hi.", None), _sse(None, "stop", usage=True), _DONE], recorded=recorded)
    async for _ in build_llm(client=oai).chat(chat_ctx=_ctx()):
        pass


# ---------------------------------------------------------------------------
# F1 — the REAL worker construction path caps outbound requests, lower-only.
# ---------------------------------------------------------------------------


def test_outbound_request_capped_at_ceiling_by_default(monkeypatch):
    """build_llm() with no override sends max_completion_tokens=64 on the wire."""
    monkeypatch.delenv("SUMI_LLM_MAX_TOKENS", raising=False)
    recorded: dict = {}
    asyncio.run(_drive_full_turn(recorded))
    assert recorded["body"]["max_completion_tokens"] == CEIL


def test_env_override_may_only_lower_the_cap(monkeypatch):
    """A valid lower override flows through the real path to the wire request."""
    monkeypatch.setenv("SUMI_LLM_MAX_TOKENS", "32")
    recorded: dict = {}
    asyncio.run(_drive_full_turn(recorded))
    assert recorded["body"]["max_completion_tokens"] == 32


def test_override_above_ceiling_fails_loud(monkeypatch):
    """SUMI_LLM_MAX_TOKENS=65536 must not construct a worker — a raisable cap is no cap."""
    monkeypatch.setenv("SUMI_LLM_MAX_TOKENS", str(CEIL + 1))
    with pytest.raises(RuntimeError, match="only LOWER"):
        build_llm(client=object())


def test_override_non_numeric_fails_loud(monkeypatch):
    monkeypatch.setenv("SUMI_LLM_MAX_TOKENS", "sixty-four")
    with pytest.raises(RuntimeError, match="integer"):
        build_llm(client=object())


def test_override_zero_fails_loud(monkeypatch):
    monkeypatch.setenv("SUMI_LLM_MAX_TOKENS", "0")
    with pytest.raises(RuntimeError, match="only LOWER"):
        build_llm(client=object())


# ---------------------------------------------------------------------------
# F2 — interrupt closes the real downstream HTTP transport stream.
# ---------------------------------------------------------------------------


def test_interrupt_closes_downstream_http_stream(monkeypatch):
    """aclose() mid-turn (what AgentSession does on disconnect) closes the httpx stream."""
    monkeypatch.delenv("SUMI_LLM_MAX_TOKENS", raising=False)

    async def go():
        oai, stream = _mock_client([_sse("Good evening.", None)], endless=True)
        turn = build_llm(client=oai).chat(chat_ctx=_ctx())
        async for _ in turn:  # receive the first emitted chunk, then interrupt
            break
        await turn.aclose()
        return stream.aclosed

    assert asyncio.run(go()) is True, (
        "downstream HTTP transport stream was NOT closed on interrupt — a cancelled turn could leak a generation"
    )
