"""Worker-side receipts for classifying LLM completion versus cancellation."""

import asyncio
import logging
from collections.abc import AsyncIterator

import pytest
from agent import _observe_llm_stream
from livekit.agents import FlushSentinel, llm


def _usage_chunk(*, completion_tokens: int = 33) -> llm.ChatChunk:
    return llm.ChatChunk(
        id="turn-1",
        usage=llm.CompletionUsage(
            completion_tokens=completion_tokens,
            prompt_tokens=20,
            total_tokens=20 + completion_tokens,
        ),
    )


def test_normal_completion_receipt_has_usage_chars_and_cap_state(caplog):
    async def source():
        yield llm.ChatChunk(
            id="turn-1",
            delta=llm.ChoiceDelta(role="assistant", content="A quiet answer."),
        )
        yield _usage_chunk(completion_tokens=33)

    async def go():
        return [chunk async for chunk in _observe_llm_stream(source(), max_tokens=64)]

    with caplog.at_level(logging.INFO, logger="voice.agent"):
        chunks = asyncio.run(go())

    assert len(chunks) == 2
    assert "outcome=completed" in caplog.text
    assert "completion_tokens=33" in caplog.text
    assert "output_chars=15" in caplog.text
    assert "cap_reached=False" in caplog.text
    assert "provider_finish_reason=unavailable" in caplog.text


def test_cap_reached_is_visible(caplog):
    async def source():
        yield _usage_chunk(completion_tokens=64)

    async def go():
        return [chunk async for chunk in _observe_llm_stream(source(), max_tokens=64)]

    with caplog.at_level(logging.INFO, logger="voice.agent"):
        chunks = asyncio.run(go())

    assert len(chunks) == 2
    assert chunks[-1] == (
        "\n\nI reached my reply limit before I could finish that. Ask me to continue."
    )
    assert "outcome=completed" in caplog.text
    assert "cap_reached=True" in caplog.text


def test_cap_notice_is_not_emitted_below_cap():
    async def source():
        yield llm.ChatChunk(
            id="turn-1",
            delta=llm.ChoiceDelta(role="assistant", content="A complete answer."),
        )
        yield _usage_chunk(completion_tokens=63)

    async def go():
        return [chunk async for chunk in _observe_llm_stream(source(), max_tokens=64)]

    chunks = asyncio.run(go())
    assert len(chunks) == 2
    assert all(not isinstance(chunk, str) for chunk in chunks)


def test_provider_error_does_not_fabricate_cap_notice(caplog):
    seen: list[llm.ChatChunk | str | FlushSentinel] = []

    async def source():
        yield llm.ChatChunk(
            id="turn-1",
            delta=llm.ChoiceDelta(role="assistant", content="An interrupted answer"),
        )
        raise RuntimeError("provider stream failed")

    async def go():
        with pytest.raises(RuntimeError, match="provider stream failed"):
            async for chunk in _observe_llm_stream(source(), max_tokens=64):
                seen.append(chunk)

    with caplog.at_level(logging.INFO, logger="voice.agent"):
        asyncio.run(go())

    assert len(seen) == 1
    assert all(not isinstance(chunk, str) for chunk in seen)
    assert "outcome=error" in caplog.text
    assert "cap_reached=False" in caplog.text


def test_pipeline_cancellation_is_distinct_from_normal_completion(caplog):
    entered = asyncio.Event()

    async def source() -> AsyncIterator[llm.ChatChunk | str | FlushSentinel]:
        entered.set()
        await asyncio.Future()
        yield _usage_chunk()  # pragma: no cover - makes this an async generator

    async def go():
        stream = _observe_llm_stream(source(), max_tokens=64)
        task = asyncio.ensure_future(anext(stream))
        await entered.wait()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

    with caplog.at_level(logging.INFO, logger="voice.agent"):
        asyncio.run(go())

    assert "outcome=cancelled" in caplog.text
    assert "cap_reached=False" in caplog.text


def test_consumer_close_is_distinct_from_normal_completion(caplog):
    async def source():
        yield llm.ChatChunk(
            id="turn-1",
            delta=llm.ChoiceDelta(role="assistant", content="unfinished"),
        )
        yield _usage_chunk(completion_tokens=33)

    async def go():
        stream = _observe_llm_stream(source(), max_tokens=64)
        await anext(stream)
        await stream.aclose()

    with caplog.at_level(logging.INFO, logger="voice.agent"):
        asyncio.run(go())

    assert "outcome=consumer_closed" in caplog.text
    assert "completion_tokens=None" in caplog.text
