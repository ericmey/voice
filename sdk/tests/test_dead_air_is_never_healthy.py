"""Eric said "hello?" four times into silence and every gate reported green.

The 2026-07-11 acceptance call: Aoi's input pipeline wedged after a turn. For 98 seconds
the container was healthy, the worker registered, SIP routing exact, `make health` green,
and the agent's state was `listening` — which is what a *working* agent looks like.

LiveKit emitted `user_state -> away`. The telemetry collector recorded it. Nothing acted.

These tests exist so that a silent call can never again be a healthy call. They drive the
watchdog on a fake clock and a fake session — no sleeping, no real timers — and require it
to actually fire.
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from typing import Any

import pytest
from sdk.liveness import wire_liveness_watchdog


class FakeClock:
    """Time I control. A watchdog tested against the real clock is a slow flaky test."""

    def __init__(self) -> None:
        self.t = 1000.0

    def __call__(self) -> float:
        return self.t

    async def advance(self, seconds: float, *, step: float = 1.0) -> None:
        """Move time forward, letting the watchdog's tick loop observe each step."""
        target = self.t + seconds
        while self.t < target:
            self.t = min(self.t + step, target)
            await asyncio.sleep(0)  # let the watchdog task run
            await asyncio.sleep(0)


class FakeSession:
    """A LiveKit AgentSession's event surface, and nothing else."""

    def __init__(self, *, generate_reply_fails: bool = False) -> None:
        self._handlers: dict[str, list[Any]] = {}
        self.replies: list[str] = []
        self.closed = False
        self.generate_reply_fails = generate_reply_fails

    def on(self, event: str):
        def _register(fn):
            self._handlers.setdefault(event, []).append(fn)
            return fn

        return _register

    def emit(self, event: str, payload: Any = None) -> None:
        for fn in self._handlers.get(event, []):
            fn(payload)

    async def generate_reply(self, *, instructions: str) -> None:
        """Emits what the REAL AgentSession emits. This is the whole point.

        The previous fake appended a string and emitted NOTHING — so the watchdog's own prompt
        looked like a no-op, and `test_prompt_once` passed against a mock that was weaker than
        production. In production, generate_reply emits agent thinking -> speaking and an
        ASSISTANT conversation_item, all of which the old watchdog treated as proof of life,
        which meant it re-armed itself and could prompt a silent caller every 25s forever.
        A mock that cannot reproduce the bug cannot certify the fix. (Yua.)
        """
        if self.generate_reply_fails:
            raise RuntimeError("realtime session is wedged: no response channel")
        self.replies.append(instructions)
        self.emit("agent_state_changed", SimpleNamespace(new_state="thinking"))
        self.emit("agent_state_changed", SimpleNamespace(new_state="speaking"))
        self.emit(
            "conversation_item_added", SimpleNamespace(item=SimpleNamespace(role="assistant"))
        )
        self.emit("agent_state_changed", SimpleNamespace(new_state="listening"))

    async def aclose(self) -> None:
        self.closed = True


def _ctx() -> Any:
    shutdowns: list[str] = []
    return SimpleNamespace(shutdown=lambda reason="": shutdowns.append(reason), shutdowns=shutdowns)


def _user_turn() -> Any:
    return SimpleNamespace(item=SimpleNamespace(role="user"))


def _agent_turn() -> Any:
    return SimpleNamespace(item=SimpleNamespace(role="assistant"))


def _state(new: str) -> Any:
    return SimpleNamespace(new_state=new)


async def _watchdog(session: FakeSession, clock: FakeClock, ctx: Any = None, **kw):
    state = wire_liveness_watchdog(
        session,
        ctx if ctx is not None else _ctx(),
        call_sid="SCL_test",
        agent_name="aoi",
        now=clock,
        tick_s=0,  # the fake clock drives time; do not sleep on the real one
        **kw,
    )
    await asyncio.sleep(0)
    return state


# --- the failure, reproduced ------------------------------------------------------


@pytest.mark.asyncio
async def test_dead_air_is_detected_and_the_agent_speaks() -> None:
    """25 seconds of nothing in either direction. She must say something."""
    clock, session = FakeClock(), FakeSession()
    state = await _watchdog(session, clock)

    session.emit("conversation_item_added", _agent_turn())
    await clock.advance(30)

    assert state.prompted, "98 seconds of dead air and the watchdog never fired"
    assert session.replies, "she detected the silence and still said nothing"
    assert state.outcome == "detected"
    assert not state.hung_up


@pytest.mark.asyncio
async def test_a_call_that_stays_dead_is_ended() -> None:
    """Silence is worse than a dropped call. A dropped call is legible."""
    clock, session = FakeClock(), FakeSession()
    ctx = _ctx()
    state = await _watchdog(session, clock, ctx=ctx)

    await clock.advance(70)

    assert state.hung_up, "the call was dead for 70s and we left the caller sitting there"
    assert state.outcome == "terminated"
    assert session.closed
    assert ctx.shutdowns, "the job was never shut down"


@pytest.mark.asyncio
async def test_a_wedged_session_that_cannot_speak_is_still_ended() -> None:
    """THE ACTUAL 2026-07-11 CASE. Re-engage FAILS because the session is wedged.

    The recovery attempt is not the safety net — the hangup is. If the only thing standing
    between the caller and 98 seconds of silence were a `generate_reply` on a broken session,
    we would have shipped a watchdog that dies of the same disease it is treating.
    """
    clock, session = FakeClock(), FakeSession(generate_reply_fails=True)
    state = await _watchdog(session, clock)

    await clock.advance(70)

    assert not session.replies, "the session was supposed to be wedged"
    assert state.hung_up, "re-engage failed and the caller was left in silence anyway"
    assert state.outcome == "terminated"


# --- the trap: the monitor must not be fed by the fault it is watching -------------


@pytest.mark.asyncio
async def test_the_agents_own_echo_cannot_keep_the_watchdog_alive() -> None:
    """THE ONE THAT MATTERS.

    On the failing call the session reported `user_state=speaking` for 57.69-73.54 — the
    *exact* window the agent was speaking (58.08-73.55). Her own audio was on the input path.

    A watchdog that treated VAD as proof of life would have been kept alive BY THE ECHO — fed
    by the very fault it exists to catch — and Eric would still have been stranded. So user
    VAD must never reset the timer. Only a real turn does.
    """
    clock, session = FakeClock(), FakeSession()
    state = await _watchdog(session, clock)

    # She starts a long turn. Her voice bleeds into the input; VAD says "user speaking".
    session.emit("agent_state_changed", _state("speaking"))
    session.emit("user_state_changed", _state("speaking"))  # <- the echo
    await clock.advance(15)
    session.emit("agent_state_changed", _state("listening"))
    session.emit("user_state_changed", _state("listening"))

    # Now the input pipeline is dead. The caller talks; nothing reaches the session.
    # Only the echo ever fired. If that counted as life, this call is silent forever.
    await clock.advance(70)

    assert state.suspected_echo_events >= 1, "the bleed was not even noticed"
    assert state.hung_up, (
        "the agent's own echo kept the watchdog alive — the monitor was fed by the fault it "
        "was watching, and the caller is still sitting in silence"
    )


@pytest.mark.asyncio
async def test_the_bleed_is_counted_and_surfaced() -> None:
    """`user_state=speaking` while the agent speaks is a diagnosis, not noise.

    Nothing counted this on the real call. It is the single clearest fingerprint of the
    wedge's cause, and it was sitting in the event stream the whole time.
    """
    clock, session = FakeClock(), FakeSession()
    state = await _watchdog(session, clock)

    for _ in range(3):
        session.emit("agent_state_changed", _state("speaking"))
        session.emit("user_state_changed", _state("speaking"))
        session.emit("user_state_changed", _state("listening"))
        session.emit("agent_state_changed", _state("listening"))

    assert state.suspected_echo_events == 3
    assert len(state.echo_windows) == 3


@pytest.mark.asyncio
async def test_user_vad_while_the_agent_is_silent_is_not_treated_as_echo() -> None:
    """A caller genuinely speaking is not a bleed. Do not cry wolf."""
    clock, session = FakeClock(), FakeSession()
    state = await _watchdog(session, clock)

    session.emit("agent_state_changed", _state("listening"))
    session.emit("user_state_changed", _state("speaking"))

    assert state.suspected_echo_events == 0


# --- normal calls must be left alone -----------------------------------------------


@pytest.mark.asyncio
async def test_a_healthy_conversation_is_never_interrupted() -> None:
    """A watchdog that fires on a working call gets disabled, and then it protects nobody."""
    clock, session = FakeClock(), FakeSession()
    state = await _watchdog(session, clock)

    for _ in range(6):
        session.emit("conversation_item_added", _user_turn())
        await clock.advance(10)
        session.emit("conversation_item_added", _agent_turn())
        await clock.advance(10)

    assert not state.prompted, "she interrupted a perfectly good conversation"
    assert not state.hung_up
    assert state.outcome == "healthy"
    assert not session.replies


@pytest.mark.asyncio
async def test_a_caller_who_comes_back_is_recovered_not_hung_up() -> None:
    """He was thinking. She asked if he was there. He answered. That is a working call."""
    clock, session = FakeClock(), FakeSession()
    state = await _watchdog(session, clock)

    await clock.advance(30)
    assert state.prompted and state.outcome == "detected"

    session.emit("conversation_item_added", _user_turn())  # "yeah, sorry — still here"
    await clock.advance(20)

    assert state.outcome == "recovered"
    assert not state.hung_up, "he came back and she hung up on him"


@pytest.mark.asyncio
async def test_the_watchdog_prompts_once_and_hangs_up_once() -> None:
    """No duplicate prompts, no duplicate hangups. (Yua, QA scope.)"""
    clock, session = FakeClock(), FakeSession()
    ctx = _ctx()
    state = await _watchdog(session, clock, ctx=ctx)

    await clock.advance(120)

    assert len(session.replies) == 1, f"she asked 'still there?' {len(session.replies)} times"
    assert len(ctx.shutdowns) == 1, "the job was shut down more than once"
    assert state.outcome == "terminated"


@pytest.mark.asyncio
async def test_the_timer_stops_on_normal_teardown() -> None:
    """A watchdog that outlives its call is a task leak that fires into a dead session."""
    clock, session = FakeClock(), FakeSession()
    state = await _watchdog(session, clock)

    session.emit("close", None)
    await asyncio.sleep(0)
    await clock.advance(120)

    assert not state.hung_up, "the watchdog kept running after the call ended"
    assert not session.replies


# --- the watchdog must not certify its own recovery --------------------------------


@pytest.mark.asyncio
async def test_the_watchdog_cannot_keep_itself_alive_by_talking() -> None:
    """THE ONE THE MOCK HID. Her re-engage emits agent events and an assistant turn.

    The old watchdog counted those as activity: the prompt reset its own deadline, cleared
    `prompted`, and marked the call RECOVERED. So a caller who had genuinely gone away would be
    asked "still there?" every 25 seconds, forever, and never hung up — the watchdog certifying
    its own recovery with its own voice.

    It passed my test only because the fake generate_reply emitted nothing. The mock was weaker
    than production, so it certified behaviour that did not exist. (Yua, reproduced.)
    """
    clock, session = FakeClock(), FakeSession()
    ctx = _ctx()
    state = await _watchdog(session, clock, ctx=ctx)

    await clock.advance(120)  # caller says NOTHING, the whole time

    assert len(session.replies) == 1, (
        f"she prompted {len(session.replies)} times — her own voice is resetting her own "
        f"deadline, and this caller is never hung up"
    )
    assert state.hung_up, "the caller was gone and the watchdog talked to itself instead"
    assert state.outcome == "terminated"
    assert state.outcome != "recovered", "she 'recovered' a call in which nobody spoke"


@pytest.mark.asyncio
async def test_only_a_real_user_turn_proves_recovery() -> None:
    """An assistant turn is the agent talking. It says nothing about whether anyone is there."""
    clock, session = FakeClock(), FakeSession()
    state = await _watchdog(session, clock)

    await clock.advance(30)
    assert state.prompted and state.outcome == "detected"

    session.emit("conversation_item_added", _agent_turn())  # she keeps talking
    await clock.advance(5)
    assert state.outcome == "detected", "an assistant turn was treated as the caller answering"

    session.emit("conversation_item_added", _user_turn())  # HE answers
    assert state.outcome == "recovered"


@pytest.mark.asyncio
async def test_a_long_agent_turn_is_not_cut_off_mid_sentence() -> None:
    """A legitimate 90-second monologue is not a dead call — but it is not proof of a caller
    either. Two clocks: hang up on caller silence, never in the middle of her sentence."""
    clock, session = FakeClock(), FakeSession()
    state = await _watchdog(session, clock)

    session.emit("conversation_item_added", _user_turn())  # he asked for a long story
    session.emit("agent_state_changed", _state("speaking"))
    await clock.advance(90)  # she is still speaking

    assert not state.hung_up, "she was cut off mid-sentence"

    session.emit("agent_state_changed", _state("listening"))
    await clock.advance(10)

    assert state.hung_up, (
        "she finished, the caller still never spoke, and the call was left open anyway — "
        "agent speech must not stand in for caller presence"
    )
