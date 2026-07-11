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

    last_activity: float
    prompted: bool = False
    hung_up: bool = False

    # DETECTED / RECOVERED / TERMINATED must be distinguishable after the fact.
    # "The call ended" and "the call ended because she stopped answering and we cut it" are
    # different events, and a post-mortem that cannot tell them apart is how a wedge gets
    # written off as a caller who hung up. (Yua, dead-air QA scope.)
    outcome: str = "healthy"  # healthy | detected | recovered | terminated
    # THE BLEED COUNTER. Every time the session believes the USER started speaking while the
    # AGENT was speaking, and no user turn ever materialises from it, that is her own voice
    # arriving on the input path. On the 2026-07-11 call this fired for the entire 15 seconds
    # of her final turn and nothing counted it. Now it is a number someone can look at.
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
    state = LivenessState(last_activity=now())

    agent_speaking = False
    user_speaking_since: float | None = None

    def _mark_alive(why: str) -> None:
        if state.hung_up:
            return  # the call is over; nothing revives it
        state.last_activity = now()
        if state.prompted:
            # She came back. Re-arm, so a second silence later in the call is caught too.
            state.outcome = "recovered"
            logger.info("liveness: RECOVERED after prompt (%s) call_sid=%s", why, call_sid)
            trace(f"liveness: recovered after prompt ({why}) call_sid={call_sid}")
        state.prompted = False

    @session.on("conversation_item_added")
    def _on_item(event: Any) -> None:
        # A REAL turn — text actually produced by, or for, a human. This is the only evidence
        # that the pipeline is end-to-end alive. Audio-level VAD is NOT: on the failing call
        # the VAD was firing happily on the agent's own echo the whole time.
        item = getattr(event, "item", None)
        role = getattr(item, "role", None)
        if role in ("user", "assistant"):
            _mark_alive(f"{role} turn")

    @session.on("agent_state_changed")
    def _on_agent_state(event: Any) -> None:
        nonlocal agent_speaking
        new = str(getattr(event, "new_state", ""))
        agent_speaking = new == "speaking"
        if new in ("speaking", "thinking"):
            _mark_alive(f"agent {new}")

    @session.on("user_state_changed")
    def _on_user_state(event: Any) -> None:
        nonlocal user_speaking_since
        new = str(getattr(event, "new_state", ""))

        if new == "speaking":
            # DO NOT mark the session alive here. This is exactly the signal that lied.
            #
            # On 2026-07-11 the session reported user_state=speaking for 57.69-73.54 — the
            # precise window the AGENT was speaking (58.08-73.55) — because her own audio was
            # on the input path. If dead-air detection trusted VAD, the wedge would have kept
            # the watchdog alive with the agent's own voice and the caller would still have
            # been stranded. The monitor would have been fed by the very fault it was watching.
            if agent_speaking:
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
                    f"liveness: suspected echo (user VAD during agent speech) "
                    f"call_sid={call_sid} count={state.suspected_echo_events}"
                )
            return

        if new == "away":
            # LiveKit's own away signal. It fired at 88.5s on the failing call, was recorded,
            # and was ignored. It is not proof of a wedge (a caller may simply be quiet), so
            # it does not end the call by itself — but it is never again merely logged.
            logger.warning(
                "liveness: user marked AWAY (call_sid=%s agent=%s) — no caller input reaching "
                "the session",
                call_sid,
                agent_name,
            )
            trace(f"liveness: user away call_sid={call_sid}")

        if user_speaking_since is not None and new == "listening":
            state.echo_windows.append((user_speaking_since, now()))
            user_speaking_since = None

    async def _reengage() -> bool:
        """Try to speak. Returns False if the session cannot — which proves the wedge."""
        try:
            await session.generate_reply(
                instructions=(
                    "You have heard nothing from the caller for a while. Check in warmly and "
                    "briefly — ask if they are still there. One short sentence."
                )
            )
            return True
        except Exception as exc:  # noqa: BLE001 — a wedged session can fail in any way
            logger.error(
                "liveness: re-engage FAILED (call_sid=%s): %s — the session is not merely "
                "quiet, it is wedged",
                call_sid,
                exc,
            )
            trace(f"liveness: re-engage failed call_sid={call_sid}: {exc}")
            return False

    async def _hang_up(silent_for: float) -> None:
        if state.hung_up:
            return  # exactly once, ever
        state.hung_up = True
        state.outcome = "terminated"
        logger.error(
            "liveness: ENDING CALL after %.0fs of dead air (call_sid=%s agent=%s echo_events=%d). "
            "A live call that answers nothing is worse than a dropped one — the caller cannot "
            "tell whether it is them.",
            silent_for,
            call_sid,
            agent_name,
            state.suspected_echo_events,
        )
        trace(
            f"liveness: hangup after {silent_for:.0f}s dead air call_sid={call_sid} "
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
            silent_for = now() - state.last_activity

            if silent_for >= hangup_after_s:
                await _hang_up(silent_for)
                return

            if silent_for >= prompt_after_s and not state.prompted:
                state.prompted = True  # exactly one prompt per silence
                state.outcome = "detected"
                logger.warning(
                    "liveness: DEAD AIR %.0fs (call_sid=%s agent=%s) — no turn in either "
                    "direction. Re-engaging.",
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
