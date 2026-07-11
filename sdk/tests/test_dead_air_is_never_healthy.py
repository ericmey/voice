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


class FakeSpeechHandle:
    """What `AgentSession.generate_reply` ACTUALLY returns.

    It is NOT a coroutine. `generate_reply` is SYNCHRONOUS and returns this handle, which is
    awaitable — and awaiting it waits for generation *and playout* to complete. That is the
    contract the old fake did not model: it could only ever complete immediately or raise
    immediately, so a handle that simply NEVER FINISHES was inexpressible — which is exactly
    how a wedged realtime channel behaves. (Yua, verified against the installed LiveKit.)
    """

    def __init__(self, *, never_completes: bool = False) -> None:
        self.never_completes = never_completes
        self.interrupted = False
        self._done = asyncio.Event()
        if not never_completes:
            self._done.set()

    def interrupt(self) -> None:
        self.interrupted = True
        self._done.set()

    def __await__(self):
        return self._done.wait().__await__()


class FakeSession:
    """A LiveKit AgentSession's event surface, and nothing else."""

    def __init__(self, *, generate_reply_fails: bool = False) -> None:
        self._handlers: dict[str, list[Any]] = {}
        self.replies: list[str] = []
        self.closed = False
        self.generate_reply_fails = generate_reply_fails
        self.reply_never_completes = False
        self.handles: list[FakeSpeechHandle] = []
        # aclose() behaviour, modelled on the installed AgentSession._aclose_impl:
        #   force-interrupt -> drain -> AWAIT activity.current_speech -> emit("close")
        #   -> MORE CLEANUP (room_io, forward-audio tasks, toolsets)
        self.aclose_never_completes = False
        self.cleanup_finished = False

    def on(self, event: str):
        def _register(fn):
            self._handlers.setdefault(event, []).append(fn)
            return fn

        return _register

    def emit(self, event: str, payload: Any = None) -> None:
        for fn in self._handlers.get(event, []):
            fn(payload)

    def generate_reply(self, *, instructions: str) -> FakeSpeechHandle:
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
        handle = FakeSpeechHandle(never_completes=self.reply_never_completes)
        self.handles.append(handle)
        if not self.reply_never_completes:
            self.emit("agent_state_changed", SimpleNamespace(new_state="listening"))
        # If it never completes she stays `speaking` FOREVER, so agent_busy stays True — which
        # is exactly how a wedged recovery could hold a dead call open if the watchdog deferred
        # to it. The old fake could not express this at all.
        return handle

    async def aclose(self) -> None:
        """Modelled on the REAL `_aclose_impl`, which the old fake did not resemble at all.

        Two things the old one-liner could not express, and both were P0s:

        1. It AWAITS uninterruptible speech (`activity.current_speech`). On a wedged realtime
           path that never returns — so an unbounded `await session.aclose()` blocks forever
           and the `finally` holding `ctx.shutdown` is never reached. The wedge simply MOVED
           from the recovery await into the close await.

        2. It emits `close` PART WAY THROUGH and then does more cleanup. If the watchdog's own
           close handler cancels the watch task, and the watch task is the one sitting inside
           `aclose()`, it cancels its own caller and the remaining teardown is aborted at the
           next await.

        (Yua, both verified against the installed LiveKit source.)
        """
        if self.aclose_never_completes:
            await asyncio.Event().wait()  # uninterruptible speech that never finishes

        self.closed = True
        self.emit("close", None)  # <- mid-close, exactly as LiveKit does
        await asyncio.sleep(0)  # the next await, where a stray cancel would land
        self.cleanup_finished = True  # room_io / forward-audio / toolsets


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


# --- the watchdog must not be wedgeable by the wedge -------------------------------


@pytest.mark.asyncio
async def test_a_recovery_that_never_completes_cannot_block_the_hangup() -> None:
    """THE P0. The watchdog awaited the thing that was broken.

    `generate_reply` is synchronous and returns an awaitable SpeechHandle; awaiting it waits
    for PLAYOUT. The old code did `await session.generate_reply(...)` inline in the watch loop.
    If the realtime channel wedges by never completing — rather than by raising — `_watch`
    blocks inside the re-engage and NEVER REACHES THE HANGUP.

    The watchdog would have been disabled by the exact failure it exists to catch, and the
    caller would sit in silence past 100 seconds with prompted=True, hung_up=False.
    """
    clock, session = FakeClock(), FakeSession()
    session.reply_never_completes = True
    ctx = _ctx()
    state = await _watchdog(session, clock, ctx=ctx)

    await clock.advance(120)

    assert session.replies, "she never even tried to re-engage"
    assert state.hung_up, (
        "the recovery speech never completed and the watchdog waited on it forever — it was "
        "disabled by the very wedge it exists to catch, and the caller is still in silence"
    )
    assert state.outcome == "terminated"
    assert session.closed
    assert len(ctx.shutdowns) == 1


@pytest.mark.asyncio
async def test_a_wedged_recovery_is_interrupted_not_left_speaking() -> None:
    """A stuck prompt must not hold the call open. Interrupt it, then close."""
    clock, session = FakeClock(), FakeSession()
    session.reply_never_completes = True
    state = await _watchdog(session, clock)

    await clock.advance(120)

    assert state.hung_up
    assert session.handles and session.handles[0].interrupted, (
        "the wedged recovery speech was never interrupted — agent_busy stays True forever and "
        "a watchdog that defers to it would hold a dead call open indefinitely"
    )


@pytest.mark.asyncio
async def test_a_wedged_recovery_does_not_earn_the_grace_real_speech_gets() -> None:
    """`agent_busy` defers the hangup for a long LEGITIMATE turn. It must not do so for the
    watchdog's own stuck voice — otherwise the watchdog holds the call open with its own
    wedge. Ordinary speech and the recovery attempt are different things."""
    clock, session = FakeClock(), FakeSession()
    session.reply_never_completes = True
    state = await _watchdog(session, clock)

    await clock.advance(120)

    # She is still "speaking" (the wedged prompt never finished) — and the call ended anyway.
    assert state.agent_busy, "the fake was supposed to leave her stuck mid-speech"
    assert state.hung_up, "the watchdog deferred to its own wedged recovery and never hung up"


@pytest.mark.asyncio
async def test_closing_during_a_wedged_recovery_does_not_double_close() -> None:
    """Close race: the caller hangs up while the stuck prompt is still in flight."""
    clock, session = FakeClock(), FakeSession()
    session.reply_never_completes = True
    ctx = _ctx()
    state = await _watchdog(session, clock, ctx=ctx)

    await clock.advance(30)  # prompt fired, handle is stuck
    assert state.prompted

    session.emit("close", None)  # he hangs up
    await clock.advance(120)

    assert not state.hung_up, "the watchdog kept running after the call closed"
    assert len(ctx.shutdowns) == 0, "it shut the job down after the session had already closed"
    assert session.handles[0].interrupted, "the in-flight recovery speech was left dangling"


# --- the hard-close boundary: the wedge must not simply MOVE -----------------------


@pytest.mark.asyncio
async def test_a_close_that_never_completes_still_brings_the_job_down() -> None:
    """P0. `aclose()` awaits uninterruptible speech — on a wedged path it never returns.

    An unbounded `await session.aclose()` means the `finally` holding `ctx.shutdown` is never
    reached: the caller is on a dead line and the job stays alive forever. The graceful close
    is best-effort. The job coming down is not optional.
    """
    clock, session = FakeClock(), FakeSession()
    session.reply_never_completes = True
    session.aclose_never_completes = True
    ctx = _ctx()
    state = await _watchdog(session, clock, ctx=ctx)

    await clock.advance(120)
    # the close is hung; give the bounded wait its real (short) timeout
    await asyncio.wait_for(_until(lambda: bool(ctx.shutdowns)), timeout=10)

    assert state.hung_up
    assert ctx.shutdowns == ["dead air — no caller input"], (
        "session.aclose() never returned and the job was never shut down — the wedge just "
        "moved from the recovery await into the close await"
    )


@pytest.mark.asyncio
async def test_the_watchdog_does_not_cancel_the_task_that_is_closing() -> None:
    """P0. `_aclose_impl` emits `close` MID-CLOSE and then keeps cleaning up.

    `_watch` is the task inside `session.aclose()`. An unconditional `task.cancel()` in the
    close handler cancels its own caller, and the CancelledError lands at the next await inside
    LiveKit's close — aborting room_io teardown, forward-audio cancellation, toolset cleanup.
    """
    clock, session = FakeClock(), FakeSession()
    ctx = _ctx()
    state = await _watchdog(session, clock, ctx=ctx)

    await clock.advance(70)
    await _settle()

    assert state.hung_up
    assert session.cleanup_finished, (
        "the close handler cancelled the very task performing the close — LiveKit's remaining "
        "teardown was aborted at the next await"
    )
    assert len(ctx.shutdowns) == 1, "shutdown must happen exactly once"


@pytest.mark.asyncio
async def test_an_external_close_still_stops_the_watchdog() -> None:
    """The caller hangs up. That close is NOT ours — the watch task must be cancelled."""
    clock, session = FakeClock(), FakeSession()
    ctx = _ctx()
    state = await _watchdog(session, clock, ctx=ctx)

    session.emit("close", None)  # LiveKit closing for its own reasons
    await _settle()
    await clock.advance(200)

    assert not state.hung_up, "the watchdog kept running after an external close"
    assert ctx.shutdowns == [], "it shut the job down on a close it did not initiate"


@pytest.mark.asyncio
async def test_abandoning_recovery_twice_is_harmless() -> None:
    """`_abandon_recovery` runs from the hangup path AND the close handler."""
    clock, session = FakeClock(), FakeSession()
    session.reply_never_completes = True
    state = await _watchdog(session, clock)

    await clock.advance(120)
    await _settle()

    assert state.hung_up
    handle = session.handles[0]
    assert handle.interrupted
    # cleared ownership: a second pass must not re-interrupt a handle we have let go of
    handle.interrupted = False
    session.emit("close", None)
    await _settle()
    assert handle.interrupted is False, "an abandoned handle was interrupted again"


async def _settle() -> None:
    for _ in range(20):
        await asyncio.sleep(0)


async def _until(pred) -> None:
    while not pred():
        await asyncio.sleep(0.05)
