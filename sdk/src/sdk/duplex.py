"""Don't listen while you talk. The speakerphone is a loop, and she was closing it.

2026-07-11, the first real call. Eric was on speakerphone. Her voice came out of his phone,
went back into his microphone, travelled up the SIP leg, and arrived at Gemini as CALLER
AUDIO. The session logged ``user_state=speaking`` for 57.69–73.54 — the exact window she was
speaking (58.08–73.55) — and then registered **nothing at all** when Eric actually spoke.

She was listening to herself. Her VAD is configured ``START_SENSITIVITY_HIGH`` ("commit to
user speech faster"), which is the worst possible setting into a loop: it latches onto the
echo instantly. She spent her whole turn believing she was being interrupted, and came out of
it with the input pipeline wedged. Eric said "hello?" four times into the silence.

THE FIX IS TO OPEN THE LOOP: while she is speaking, her microphone is closed.

That is half-duplex — the oldest remedy in telephony, and the one Eric remembered before I
found it. It is not a heuristic that tries to *recognise* her own voice and subtract it; it
removes the path. There is nothing to recognise, because nothing arrives.

WHY NOT NOISE CANCELLATION? Because we cannot have it. LiveKit's enhanced noise cancellation
(``BVCTelephony``) is the documented answer for exactly this, and its package metadata says
plainly: **"Requires LiveKit Cloud."** We self-host on mizuki. I checked the wheel rather than
assume, and it is not available to us. (If the stack ever moves to Cloud, BVC is strictly
better than this — it cancels the echo without closing the mic, so barge-in survives.)

THE COST, STATED PLAINLY: while she is speaking, you cannot interrupt her.

That is a real loss and I am not going to pretend otherwise. But barge-in was already broken
on this path — the call recorded ``interruptions: 0`` — and the choice is not
"barge-in vs. no barge-in". It is "no barge-in" vs. "no barge-in AND the call wedges into 98
seconds of silence". A caller who must wait for her to finish is inconvenienced. A caller
talking to a dead line is abandoned.

``RELEASE_DELAY_S`` matters as much as the gate: audio does not stop at the instant she does.
Reopening the mic the moment her turn ends lets the tail — the last syllable still coming out
of the speaker, plus room reverb — straight back in, which is the same bug with a shorter
window.
"""

from __future__ import annotations

import asyncio
import logging
import math
import os
import time
from dataclasses import dataclass
from typing import Any

from sdk.trace import trace

logger = logging.getLogger("voice.agent")

# How long to keep the caller's mic closed AFTER she stops speaking. Covers the audio still
# in flight down the SIP leg and coming out of the speaker, plus room reverb. Too short and
# the echo tail re-opens the loop; too long and we clip the caller's first word.
# 0.4s IS A GUESS. I have never measured the real echo tail on this trunk: how long her audio
# keeps arriving at his microphone after LiveKit says she stopped. Too short and the loop
# reopens on the tail; too long and we clip the caller's first word. It is env-tunable and
# counted precisely so the next speakerphone call can MEASURE it instead of inheriting my
# guess. (Yua: "remains unmeasured".)
DEFAULT_RELEASE_DELAY_S = 0.4

# A tail may not be negative, NaN, or infinite — and it must not be parsed with a bare
# `float()` at import time, which would crash the whole agent on a typo in an env var, or
# silently accept `inf` (mic never reopens: the caller is muted for the entire call) or a
# negative (reopen fires immediately: the echo loop is back). Config errors must fail LOUDLY
# and at a boundary, not detonate at import or degrade in silence. (Yua, precision note.)
MAX_RELEASE_DELAY_S = 5.0

ENV_TAIL = "VOICE_HALF_DUPLEX_TAIL_S"


def resolve_release_delay(raw: str | None = None) -> float:
    """Parse + validate the echo-tail delay. Raises on garbage; clamps what is merely silly."""
    if raw is None:
        raw = os.environ.get(ENV_TAIL)
    if raw is None or not str(raw).strip():
        return DEFAULT_RELEASE_DELAY_S

    try:
        value = float(str(raw).strip())
    except (TypeError, ValueError):
        raise ValueError(
            f"{ENV_TAIL}={raw!r} is not a number. This is the echo-tail delay that keeps the "
            f"agent from hearing her own voice; a typo here must not start the agent."
        ) from None

    if math.isnan(value) or math.isinf(value):
        raise ValueError(
            f"{ENV_TAIL}={raw!r} is not a finite number. An infinite tail never reopens the "
            f"caller's microphone — he would be muted for the entire call."
        )

    if value < 0:
        raise ValueError(
            f"{ENV_TAIL}={raw!r} is negative. A negative tail reopens the mic immediately, "
            f"which is the echo loop this module exists to break."
        )

    if value > MAX_RELEASE_DELAY_S:
        logger.warning(
            "%s=%s exceeds the %.1fs ceiling — clamping. A tail this long clips the caller's "
            "first words after every single turn.",
            ENV_TAIL,
            raw,
            MAX_RELEASE_DELAY_S,
        )
        return MAX_RELEASE_DELAY_S

    return value


ENV_FLAG = "VOICE_HALF_DUPLEX"


def half_duplex_enabled() -> bool:
    return (os.environ.get(ENV_FLAG, "1").strip().lower()) not in ("0", "false", "no", "off")


@dataclass
class DuplexState:
    """Observable, so a call can be audited afterwards rather than guessed about."""

    enabled: bool
    gate_closures: int = 0
    input_open: bool = True


def wire_half_duplex(
    session: Any,
    *,
    call_sid: str | None,  # None on a non-SIP job; only ever used for logging
    agent_name: str,
    enabled: bool | None = None,
    release_delay_s: float | None = None,
    sleep: Any = asyncio.sleep,
) -> DuplexState:
    """Close the caller's mic while the agent speaks. Returns state for tests/telemetry."""
    is_on = half_duplex_enabled() if enabled is None else enabled
    release_delay_s = resolve_release_delay() if release_delay_s is None else release_delay_s
    state = DuplexState(enabled=is_on)

    if not is_on:
        logger.warning(
            "half-duplex DISABLED (%s=0) for call_sid=%s — the agent will hear her own audio "
            "back through a speakerphone, and that is what wedged the 2026-07-11 call",
            ENV_FLAG,
            call_sid,
        )
        return state

    # TURN GENERATION. Not a bare task handle — a *generation counter*.
    #
    # The first version kept `release_task` and checked `is not None`. After the first reopen
    # the task was DONE but still non-None, so the second turn's `listening` never scheduled a
    # reopen — and the caller stayed muted for the rest of the call. Two turns in:
    # history=[False, True, False], audio_enabled=False. My fix was worse than the bug it
    # fixed, for any conversation longer than one exchange, and my test only ever ran ONE turn.
    # (Yua, reproduced exactly.)
    #
    # A generation counter makes staleness explicit instead of inferring it from a handle's
    # liveness: every time she starts speaking, the generation advances, and a reopen that
    # belongs to an older generation simply declines to act. A cancelled-but-still-scheduled
    # task can no longer reopen the mic in the middle of a new turn, which is the race the
    # handle-based version could not even express.
    generation = 0
    release_task: asyncio.Task[None] | None = None

    def _set_input(open_: bool) -> None:
        try:
            session.input.set_audio_enabled(open_)
            state.input_open = open_
        except Exception as exc:  # noqa: BLE001 — never let the gate kill the call
            logger.error("half-duplex: could not toggle input audio: %s", exc)

    async def _reopen_after_tail(my_generation: int) -> None:
        # The tail is the whole point. Her last syllable is still leaving his speaker when
        # LiveKit says she stopped; reopening now feeds it straight back in.
        try:
            await sleep(release_delay_s)
        except asyncio.CancelledError:
            return
        if my_generation != generation:
            return  # she started speaking again — this reopen belongs to a turn that is over
        _set_input(True)
        trace(f"half-duplex: mic reopened after {release_delay_s:.2f}s tail call_sid={call_sid}")

    @session.on("agent_state_changed")
    def _on_agent_state(event: Any) -> None:
        nonlocal release_task, generation
        new = str(getattr(event, "new_state", ""))

        if new == "speaking":
            generation += 1  # any pending reopen is now stale, by construction
            if release_task is not None and not release_task.done():
                release_task.cancel()
            release_task = None
            if state.input_open:
                state.gate_closures += 1
                _set_input(False)
                trace(f"half-duplex: mic closed (agent speaking) call_sid={call_sid}")
            return

        # She stopped. Schedule the reopen for THIS generation. No `is None` guard on the
        # handle — the generation decides validity, so a second, third, tenth turn all work.
        if not state.input_open:
            if release_task is not None and not release_task.done():
                release_task.cancel()
            release_task = asyncio.create_task(
                _reopen_after_tail(generation), name=f"half-duplex-release-{call_sid}"
            )

    @session.on("close")
    def _on_close(_event: Any = None) -> None:
        nonlocal release_task
        if release_task is not None and not release_task.done():
            release_task.cancel()
        # Leave input enabled on the way out; a closed mic must never outlive the gate.
        _set_input(True)
        logger.info(
            "half-duplex: call ended (call_sid=%s agent=%s, mic closed %d times)",
            call_sid,
            agent_name,
            state.gate_closures,
        )

    logger.info(
        "half-duplex ENABLED for call_sid=%s agent=%s (release tail %.2fs) — the caller cannot "
        "interrupt while she speaks; this is what keeps her own audio off the input path",
        call_sid,
        agent_name,
        release_delay_s,
    )
    return state


# Kept deliberately: the moment this stack moves to LiveKit Cloud, this is the better fix.
BVC_NOTE = (
    "livekit-plugins-noise-cancellation (BVCTelephony) cancels far-end echo WITHOUT closing "
    "the mic, so barge-in survives. Its metadata states 'Requires LiveKit Cloud' — verified "
    "against the wheel on 2026-07-11, not assumed. We self-host, so it is unavailable. If the "
    "deployment ever moves to Cloud: pass noise_cancellation=BVCTelephony() in RoomInputOptions "
    "and half-duplex can be turned off."
)


def time_now() -> float:  # pragma: no cover - trivial seam for tests
    return time.monotonic()
