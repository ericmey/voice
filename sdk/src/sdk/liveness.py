"""A call that has gone silent must never look like a call that is fine.

2026-07-11, the first real call. Aoi answered, talked with Eric for a minute, finished a
turn — and then her input pipeline died. Eric asked his next question. Nothing. He asked
*"Any thoughts there?"*, then *"Are we?"*, then *"Are you there?"*, then *"Hello, hello?"*
He sat in silence for **98 seconds** and hung up.

For all 98 of those seconds:

- the container was ``healthy``;
- the worker was registered;
- SIP routing matched the validated set exactly;
- ``make health`` printed green on every line;
- the agent's own state was ``listening``, which is what a working agent looks like.

Every instrument built that day reported that nothing was wrong. LiveKit even *emitted the
signal* — ``user_state -> away`` at t=88.5s — and the telemetry collector faithfully
**recorded** it. Not one line of code **acted** on it.

That is the failure this module exists to make impossible. Not the bleed that caused the
wedge; the SILENCE ABOUT THE WEDGE. A different root cause will produce the same 98 seconds
tomorrow, and the caller will still be the only monitor that noticed.

THE WATCHDOG RUNS ON ITS OWN CLOCK, DELIBERATELY.

A wedged session's input is dead — so anything that waits to be *told* it has gone quiet is
asking the broken component whether it is broken. The whole defect class here is instruments
that cannot report their own failure. This one holds a timer, and the timer does not depend
on the thing it is watching.

Escalation, in order:

1. **Say so.** A loud WARNING the moment dead air crosses the threshold — because "we found
   out when Eric told us" is not monitoring.
2. **Re-engage once.** Try to speak. If the session still works (the user simply went quiet),
   this is the right, humane thing anyway: *"still there?"* If the session is wedged, it fails
   — and that failure is itself the proof, logged.
3. **End the call.** Silence is worse than a dropped call. A dropped call is legible; a live
   call that answers nothing makes the caller doubt themselves, and Eric said "hello?" four
   times into a room where nobody was home.
"""

from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass, field
from typing import Any

from sdk.trace import trace

logger = logging.getLogger("voice.agent")

# Dead air before we say something. Long enough that a normal thinking pause or a quiet
# moment is not treated as a fault; short enough that a human has not yet started wondering
# whether the line dropped. Eric's first "any thoughts there?" came ~39s into the silence.
DEAD_AIR_PROMPT_S = 25.0

# Dead air before we end the call. Measured from the last real activity, not from the prompt.
DEAD_AIR_HANGUP_S = 60.0

# How often the timer checks. Cheap; this is a clock, not a probe.
TICK_S = 2.0


@dataclass
class LivenessState:
    """What the watchdog knows. Exposed so tests can assert on it directly."""

    # THE CALLER'S CLOCK. Advanced ONLY by a real user turn.
    #
    # The first version had ONE clock, advanced by any activity — including the agent's own.
    # So the re-engage prompt ("still there?") emitted agent thinking/speaking and an assistant
    # conversation_item, which reset the clock, cleared `prompted`, and marked the call
    # RECOVERED. A caller who had genuinely gone away would be prompted every 25 seconds
    # FOREVER and never hung up: the watchdog kept certifying its own recovery with its own
    # voice. My test passed only because FakeSession.generate_reply emitted none of the events
    # the real one does — the mock was weaker than production, so it certified behaviour that
    # did not exist. (Yua, reproduced.)
    #
    # Only the caller can prove the caller is there.
    caller_last_seen: float

    # THE AGENT'S CLOCK, kept separate and used for exactly one thing: never cut her off in
    # the middle of a sentence. A long, legitimate monologue must not trip a hangup — but it
    # must not count as the caller being present either.
    agent_busy: bool = False

    prompted: bool = False
    hung_up: bool = False

    # DETECTED / RECOVERED / TERMINATED must be distinguishable after the fact. "The call
    # ended" and "the call ended because she stopped answering and we cut it" are different
    # events, and a post-mortem that cannot tell them apart writes a wedge off as a caller who
    # hung up. (Yua, dead-air QA scope.)
    outcome: str = "healthy"  # healthy | detected | recovered | terminated
    prompts_sent: int = 0

    # THE BLEED COUNTER. Every time the session believes the USER started speaking while the
    # AGENT was speaking, that is her own voice arriving on the input path. On the 2026-07-11
    # call this was true for the entire 15 seconds of her final turn and nothing counted it.
    suspected_echo_events: int = 0
    echo_windows: list[tuple[float, float]] = field(default_factory=list)


def wire_liveness_watchdog(
    session: Any,
    ctx: Any,
    *,
    call_sid: str | None,  # None on a non-SIP job; only ever used for logging
    agent_name: str,
    prompt_after_s: float = DEAD_AIR_PROMPT_S,
    hangup_after_s: float = DEAD_AIR_HANGUP_S,
    tick_s: float = TICK_S,
    now: Any = time.monotonic,
) -> LivenessState:
    """Watch for dead air. Speak once, then end the call. Never sit silent.

    Returns the ``LivenessState`` so callers (and tests) can inspect what happened.
    """
    state = LivenessState(caller_last_seen=now())

    user_speaking_since: float | None = None

    def _caller_spoke() -> None:
        """The ONLY thing that proves the caller is there. Nothing the agent does counts."""
        if state.hung_up:
            return
        state.caller_last_seen = now()
        if state.prompted:
            state.outcome = "recovered"
            logger.info("liveness: RECOVERED — caller answered call_sid=%s", call_sid)
            trace(f"liveness: recovered (caller turn) call_sid={call_sid}")
        state.prompted = False

    @session.on("conversation_item_added")
    def _on_item(event: Any) -> None:
        item = getattr(event, "item", None)
        role = getattr(item, "role", None)
        # ONLY a user turn. An assistant item is the agent talking — very possibly the
        # watchdog's own prompt — and it proves nothing about whether anyone is listening.
        if role == "user":
            _caller_spoke()

    @session.on("agent_state_changed")
    def _on_agent_state(event: Any) -> None:
        # Tracks whether she is mid-sentence. Deliberately does NOT touch caller_last_seen.
        state.agent_busy = str(getattr(event, "new_state", "")) in ("speaking", "thinking")

    @session.on("user_state_changed")
    def _on_user_state(event: Any) -> None:
        nonlocal user_speaking_since
        new = str(getattr(event, "new_state", ""))

        if new == "speaking":
            # DO NOT mark the caller alive here. This is exactly the signal that lied.
            #
            # On 2026-07-11 the session reported user_state=speaking for 57.69-73.54 — the
            # precise window the AGENT was speaking (58.08-73.55) — because her own audio was
            # on the input path. A watchdog that trusted VAD would have been kept alive BY THE
            # ECHO, fed by the very fault it exists to catch, and Eric would still have been
            # stranded in silence.
            if state.agent_busy:
                state.suspected_echo_events += 1
                user_speaking_since = now()
                logger.warning(
                    "liveness: SUSPECTED AUDIO BLEED — user VAD fired while the agent was "
                    "speaking (call_sid=%s agent=%s count=%d). The session may be hearing its "
                    "own output as caller input.",
                    call_sid,
                    agent_name,
                    state.suspected_echo_events,
                )
                trace(
                    f"liveness: suspected echo call_sid={call_sid} "
                    f"count={state.suspected_echo_events}"
                )
            return

        if new == "away":
            # LiveKit's own away signal. It fired at 88.5s on the failing call, was recorded,
            # and was ignored. Never again merely logged — though it does not end the call by
            # itself, because a caller may simply be quiet.
            logger.warning(
                "liveness: user marked AWAY (call_sid=%s agent=%s) — no caller input is "
                "reaching the session",
                call_sid,
                agent_name,
            )
            trace(f"liveness: user away call_sid={call_sid}")

        if user_speaking_since is not None and new == "listening":
            state.echo_windows.append((user_speaking_since, now()))
            user_speaking_since = None

    async def _reengage() -> None:
        """Ask once whether he is still there. This does NOT reset the caller's clock."""
        try:
            await session.generate_reply(
                instructions=(
                    "You have heard nothing from the caller for a while. Check in warmly and "
                    "briefly — ask if they are still there. One short sentence."
                )
            )
        except Exception as exc:  # noqa: BLE001 — a wedged session can fail in any way
            logger.error(
                "liveness: re-engage FAILED (call_sid=%s): %s — the session is not merely "
                "quiet, it is wedged",
                call_sid,
                exc,
            )
            trace(f"liveness: re-engage failed call_sid={call_sid}: {exc}")

    async def _hang_up(silent_for: float) -> None:
        if state.hung_up:
            return  # exactly once, ever
        state.hung_up = True
        state.outcome = "terminated"
        logger.error(
            "liveness: ENDING CALL after %.0fs with no caller input (call_sid=%s agent=%s "
            "echo_events=%d). A live call that answers nothing is worse than a dropped one — "
            "the caller cannot tell whether it is them.",
            silent_for,
            call_sid,
            agent_name,
            state.suspected_echo_events,
        )
        trace(
            f"liveness: hangup after {silent_for:.0f}s no-caller call_sid={call_sid} "
            f"echo_events={state.suspected_echo_events}"
        )
        try:
            await session.aclose()
        except Exception as exc:  # noqa: BLE001
            logger.error("liveness: session.aclose() failed: %s", exc)
        finally:
            shutdown = getattr(ctx, "shutdown", None)
            if callable(shutdown):
                shutdown(reason="dead air — no caller input")

    async def _watch() -> None:
        while not state.hung_up:
            await asyncio.sleep(tick_s)
            silent_for = now() - state.caller_last_seen

            if silent_for >= hangup_after_s:
                if state.agent_busy:
                    # She is mid-sentence. Do not cut her off — a long legitimate turn is not
                    # a dead call. Re-check on the next tick.
                    continue
                await _hang_up(silent_for)
                return

            if state.agent_busy:
                # SHE IS SPEAKING, so the line is not silent. Dead air means SILENCE — and
                # interrupting her own sentence to ask "are you still there?" is both absurd
                # and, on the failing call's realtime path, a way to wedge her further.
                # The caller's clock keeps running; we simply do not act while she talks.
                continue

            if silent_for >= prompt_after_s and not state.prompted:
                state.prompted = True  # exactly one prompt per silence; only a USER turn clears it
                state.prompts_sent += 1
                state.outcome = "detected"
                logger.warning(
                    "liveness: DEAD AIR %.0fs with no caller input (call_sid=%s agent=%s) — "
                    "re-engaging once. The clock is NOT reset by my own voice.",
                    silent_for,
                    call_sid,
                    agent_name,
                )
                trace(f"liveness: dead air {silent_for:.0f}s call_sid={call_sid}, re-engaging")
                await _reengage()

    task = asyncio.create_task(_watch(), name=f"liveness-{call_sid}")

    @session.on("close")
    def _on_close(_event: Any = None) -> None:
        task.cancel()
        if state.suspected_echo_events:
            logger.warning(
                "liveness: call ended with %d suspected audio-bleed events (call_sid=%s). The "
                "session was hearing its own output as caller input.",
                state.suspected_echo_events,
                call_sid,
            )

    return state
