"""She must not hear herself. On a speakerphone, that means closing the mic while she talks.

The 2026-07-11 call, from the audio (not from the agent's own telemetry, which lied):

    57.8-73.2   AOI speaking
    57.69-73.54 session reports user_state=SPEAKING   <- her own voice, echoed back
    73.2-78.6   ERIC actually speaks                  <- session registers NOTHING
    then        98 seconds of silence

Eric was on speakerphone. Her audio left his handset, re-entered his microphone, and arrived
at Gemini as caller input. These tests hold the loop open.
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from typing import Any

import pytest
from sdk.duplex import wire_half_duplex


class FakeInput:
    def __init__(self) -> None:
        self.audio_enabled = True
        self.history: list[bool] = []

    def set_audio_enabled(self, enable: bool) -> None:
        self.audio_enabled = enable
        self.history.append(enable)


class FakeSession:
    def __init__(self) -> None:
        self._handlers: dict[str, list[Any]] = {}
        self.input = FakeInput()

    def on(self, event: str):
        def _register(fn):
            self._handlers.setdefault(event, []).append(fn)
            return fn

        return _register

    def emit(self, event: str, payload: Any = None) -> None:
        for fn in self._handlers.get(event, []):
            fn(payload)


def _state(new: str) -> Any:
    return SimpleNamespace(new_state=new)


async def _flush() -> None:
    for _ in range(6):
        await asyncio.sleep(0)


@pytest.mark.asyncio
async def test_the_mic_is_closed_while_she_speaks() -> None:
    """THE FIX. Her voice cannot come back in if nothing is listening."""
    session = FakeSession()
    state = wire_half_duplex(
        session, call_sid="SCL_t", agent_name="aoi", enabled=True, sleep=lambda _s: asyncio.sleep(0)
    )

    session.emit("agent_state_changed", _state("speaking"))

    assert session.input.audio_enabled is False, (
        "she is speaking and the caller's mic is still open — on a speakerphone that is a "
        "closed loop, and it is what wedged the first real call"
    )
    assert state.gate_closures == 1


@pytest.mark.asyncio
async def test_the_mic_reopens_after_she_finishes() -> None:
    """Half-duplex, not mute. He has to be able to answer her."""
    session = FakeSession()
    wire_half_duplex(
        session, call_sid="SCL_t", agent_name="aoi", enabled=True, sleep=lambda _s: asyncio.sleep(0)
    )

    session.emit("agent_state_changed", _state("speaking"))
    session.emit("agent_state_changed", _state("listening"))
    await _flush()

    assert session.input.audio_enabled is True, "her turn ended and the caller is still muted"


@pytest.mark.asyncio
async def test_the_mic_stays_shut_through_the_echo_tail() -> None:
    """Her last syllable is still leaving his speaker when LiveKit says she stopped.

    Reopening at that instant lets the tail back in — the same bug through a narrower window.
    So the reopen is deliberately delayed, and the delay must actually be waited on.
    """
    session = FakeSession()
    released = asyncio.Event()

    async def _slow_sleep(_s: float) -> None:
        await released.wait()

    wire_half_duplex(session, call_sid="SCL_t", agent_name="aoi", enabled=True, sleep=_slow_sleep)

    session.emit("agent_state_changed", _state("speaking"))
    session.emit("agent_state_changed", _state("listening"))
    await _flush()

    assert session.input.audio_enabled is False, "the mic reopened before the echo tail passed"

    released.set()
    await _flush()
    assert session.input.audio_enabled is True


@pytest.mark.asyncio
async def test_a_new_turn_cancels_a_pending_reopen() -> None:
    """She pauses and speaks again. The mic must not pop open in the gap."""
    session = FakeSession()
    released = asyncio.Event()

    async def _slow_sleep(_s: float) -> None:
        await released.wait()

    wire_half_duplex(session, call_sid="SCL_t", agent_name="aoi", enabled=True, sleep=_slow_sleep)

    session.emit("agent_state_changed", _state("speaking"))
    session.emit("agent_state_changed", _state("thinking"))  # reopen scheduled
    session.emit("agent_state_changed", _state("speaking"))  # ...and she starts again
    released.set()
    await _flush()

    assert session.input.audio_enabled is False, (
        "the pending reopen fired even though she started speaking again — the loop is open "
        "for the rest of her turn"
    )


@pytest.mark.asyncio
async def test_the_mic_is_never_left_closed_when_the_call_ends() -> None:
    """A gate that outlives its call is a mute button nobody can find."""
    session = FakeSession()
    wire_half_duplex(
        session, call_sid="SCL_t", agent_name="aoi", enabled=True, sleep=lambda _s: asyncio.sleep(0)
    )

    session.emit("agent_state_changed", _state("speaking"))
    session.emit("close", None)
    await _flush()

    assert session.input.audio_enabled is True


@pytest.mark.asyncio
async def test_half_duplex_can_be_turned_off_and_says_so_loudly() -> None:
    """Turning it off is allowed. Doing it silently is not — this is the setting that decides
    whether she can hear herself."""
    session = FakeSession()
    state = wire_half_duplex(session, call_sid="SCL_t", agent_name="aoi", enabled=False)

    session.emit("agent_state_changed", _state("speaking"))

    assert state.enabled is False
    assert session.input.audio_enabled is True, "disabled means disabled"
    assert session.input.history == [], "it should not have touched the input at all"


@pytest.mark.asyncio
async def test_a_toggle_failure_never_kills_the_call() -> None:
    """If the input cannot be gated, we lose echo protection — we do not lose the call."""
    session = FakeSession()

    def _boom(_enable: bool) -> None:
        raise RuntimeError("input not attached")

    session.input.set_audio_enabled = _boom  # type: ignore[method-assign]

    wire_half_duplex(
        session, call_sid="SCL_t", agent_name="aoi", enabled=True, sleep=lambda _s: asyncio.sleep(0)
    )
    session.emit("agent_state_changed", _state("speaking"))  # must not raise
