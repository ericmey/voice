"""Integration tests for Nyla agent — LLM + tools, no audio, no rooms.

Uses LiveKit's built-in AgentSession test harness:
  session.start(agent, capture_run=True)  → captures on_enter greeting
  session.run(user_input="...")           → drives a turn, waits for completion

Requires GOOGLE_API_KEY in the environment (talks to real Gemini).
All tools run for real — no mocks. Tests hit Qdrant, NWS, Discord, etc.

Tests are grouped into small sessions (2-3 turns each) so they stay well
under Gemini's 10-minute WebSocket limit. Each test gets its own session —
sequential execution means no rapid teardown/creation noise.
A cleanup fixture prunes any Musubi memories created during the run.
"""

from __future__ import annotations

import asyncio
import os
import sys
import time
from pathlib import Path

import aiohttp
import pytest
import pytest_asyncio
from sdk.musubi_client import MUSUBI_COLLECTION, qdrant_url

# Add src/ to path so imports work
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from _shared import NylaAgent, build_model, build_tools, load_env_once, load_persona
from livekit.agents import AgentSession
from livekit.agents.voice.run_result import ChatMessageEvent, FunctionCallEvent

# Ensure env is loaded (GOOGLE_API_KEY etc.)
load_env_once()

# Opt-in gate. These tests drive a real Gemini session and every tool
# runs against production — Discord messages get sent, Musubi memories
# get written, cron jobs get scheduled, images get rendered. Dev boxes
# have GOOGLE_API_KEY set by default, so keying off that alone would let
# `pytest` create real side effects on every local run. Require an
# explicit opt-in instead.
pytestmark = pytest.mark.skipif(
    os.environ.get("RUN_INTEGRATION_TESTS") != "1",
    reason="integration tests hit real services (Discord, Musubi, cron); "
    "set RUN_INTEGRATION_TESTS=1 to opt in",
)

# Pause between turns — lets the model finish speaking before the next
# question, just like a real caller listens before responding.
TURN_GAP = 3  # seconds


# -- Helpers ---------------------------------------------------------------


def _make_agent(**kwargs) -> NylaAgent:
    """Create a NylaAgent with default config."""
    return NylaAgent(
        instructions=load_persona(),
        caller_from="+10000000000",
        extra_tools=build_tools(),
        **kwargs,
    )


def _assert_greeting_active(greeting) -> None:
    """Assert the greeting produced activity — speech or a tool call.

    The model may call musubi_recent on startup (per prompt) before
    speaking, so we accept either a message or a function call.
    """
    has_message = any(isinstance(e, ChatMessageEvent) for e in greeting.events)
    has_function_call = any(isinstance(e, FunctionCallEvent) for e in greeting.events)
    assert has_message or has_function_call, (
        f"Greeting produced no activity. Events: {greeting.events}"
    )


# -- Fixtures --------------------------------------------------------------


@pytest.fixture
def agent():
    return _make_agent()


@pytest_asyncio.fixture(autouse=True)
async def cleanup_musubi():
    """Delete any Musubi memories created by nyla-voice during the test run."""
    start_epoch = time.time()
    yield
    try:
        async with aiohttp.ClientSession() as http:
            async with http.post(
                f"{qdrant_url()}/collections/{MUSUBI_COLLECTION}/points/delete",
                json={
                    "filter": {
                        "must": [
                            {"key": "agent", "match": {"value": "nyla-voice"}},
                            {"key": "created_epoch", "range": {"gte": start_epoch}},
                        ]
                    }
                },
                params={"wait": "true"},
                timeout=aiohttp.ClientTimeout(total=5),
            ) as resp:
                if resp.status == 200:
                    data = await resp.json()
                    status = data.get("status")
                    if status != "ok":
                        print(f"[cleanup] Qdrant delete status: {status}")
                else:
                    print(f"[cleanup] Qdrant delete failed: {resp.status}")
    except Exception as err:
        print(f"[cleanup] Musubi cleanup failed: {err}")


# -- Tests -----------------------------------------------------------------
# Split into small test methods (2-3 turns each) to stay under Gemini's
# 10-minute WebSocket timeout. Each test gets its own session.


class TestCoreTools:
    """Test core tools: get_current_time, get_weather."""

    @pytest.mark.asyncio
    async def test_time_and_weather(self, agent):
        async with AgentSession(llm=build_model()) as session:
            greeting = await session.start(agent, capture_run=True)
            await greeting
            _assert_greeting_active(greeting)
            await asyncio.sleep(TURN_GAP)

            r = await session.run(user_input="What time is it right now?")
            await r
            r.expect.contains_function_call(name="get_current_time")
            await asyncio.sleep(TURN_GAP)

            r = await session.run(user_input="What's the weather like outside?")
            await r
            r.expect.contains_function_call(name="get_weather")


class TestMemoryTools:
    """Test memory tools: musubi_recent, musubi_remember.

    musubi_recent is validated by the greeting (model calls it on startup).
    This test focuses on explicit recall and store.
    """

    @pytest.mark.asyncio
    async def test_recall_and_store(self, agent):
        async with AgentSession(llm=build_model()) as session:
            greeting = await session.start(agent, capture_run=True)
            await greeting
            _assert_greeting_active(greeting)
            # Greeting should have called musubi_recent (per prompt)
            greeting.expect.contains_function_call(name="musubi_recent")
            await asyncio.sleep(TURN_GAP)

            r = await session.run(
                user_input="Remember this — I have a dentist appointment next Tuesday at 2pm."
            )
            await r
            r.expect.contains_function_call(name="musubi_remember")


class TestDelegationTools:
    """Test default delegation through OpenClaw."""

    # test_delegate_and_images removed 2026-07-08 — openclaw_delegate was
    # deleted when the voice agents were made standalone (no legacy gateway).


class TestCallbackTool:
    """Test schedule_callback."""

    @pytest.mark.skip(
        reason="schedule_callback is currently disabled — cron payload "
        "routes through a spawned agent + prose which we're replacing "
        "with a structured CLI path. See SDK TODO.md for the re-enable plan."
    )
    @pytest.mark.asyncio
    async def test_schedule_callback(self, agent):
        async with AgentSession(llm=build_model()) as session:
            greeting = await session.start(agent, capture_run=True)
            await greeting
            _assert_greeting_active(greeting)
            await asyncio.sleep(TURN_GAP)

            r = await session.run(
                user_input="Call me back in 30 minutes to remind me about the deploy."
            )
            await r
            r.expect.contains_function_call(name="schedule_callback")
