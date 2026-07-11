"""Caller info resolver for SIP-trunked voice calls.

``livekit-sip`` creates the room and the caller joins as a ``SIP`` kind
participant before the agent's entrypoint runs. Caller identity normally
lives on ``participant.attributes`` keyed by ``sip.from``, ``sip.callID``,
``sip.trunkPhoneNumber``. Some deployments omit ``sip.from`` while still
using the caller number in the participant identity, for example
``sip_+14155551234``.

``resolve_caller()`` reads those attributes and returns a
``CallerInfo`` for the agent's entrypoint to use. Call it AFTER
``await ctx.connect()`` — we need a connected room to read remote
participants.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from typing import Literal

from livekit import rtc
from livekit.agents import JobContext

logger = logging.getLogger("voice.telephony")

# Default wait budget for a SIP participant to show up after the agent
# joins the room. In practice SIP participants arrive before the agent
# does (livekit-sip creates the room + participant, then dispatches the
# agent), so this timeout is rarely exercised — it's just a safety net
# so the entrypoint can't hang forever on a misconfigured dispatch.
DEFAULT_SIP_WAIT_SECONDS = 5.0

CallerSource = Literal["sip", "unknown"]


@dataclass(frozen=True)
class CallerInfo:
    """Normalized caller information.

    All fields are best-effort — callers should handle ``None``
    gracefully. ``source`` is always set.
    """

    call_id: str | None
    """Unique identifier for this call — the SIP ``Call-ID`` header."""

    caller_from: str | None
    """Caller's phone number in E.164, if available."""

    dialed_number: str | None
    """The DID the caller reached (the 'to' number)."""

    source: CallerSource
    """``"sip"`` when the SIP participant was resolved, ``"unknown"``
    when neither a participant nor dispatch metadata produced
    identifying info within the wait budget (rare; logged as a warning).
    """


async def _wait_for_sip_participant(
    ctx: JobContext,
    timeout: float,
) -> rtc.RemoteParticipant | None:
    """Wait for the SIP caller, event-driven, bounded.

    This was a 50ms polling loop over ``room.remote_participants``. LiveKit already provides
    ``ctx.wait_for_participant(kind=...)``, which is event-driven and does the kind-filtering
    natively — so the loop was re-implementing the framework.

    **Correcting an audit finding rather than repeating it:** this was reported as "up to 5s
    of dead air before the session starts". That is wrong, and worth saying plainly. The 5s is
    the *timeout*, and it applies to either implementation — if no SIP participant ever
    arrives, both wait the full budget. The polling loop's real cost was its 50ms granularity:
    a caller who joins just after a scan waits up to 50ms extra. Not 5 seconds. Fifty
    milliseconds.

    It is still worth replacing — event-driven wakes immediately, and deleting a hand-rolled
    version of something the framework ships is a straight win. But it is a tidy-up, not the
    latency bug it was billed as, and shipping it under the wrong justification would leave
    the next person hunting a 5-second stall that was never there.

    ``ctx.wait_for_participant`` has **no timeout** — it waits forever — so it cannot be a
    drop-in. Bounded here with ``asyncio.wait_for``: the caller must fall back to
    ``source="unknown"`` rather than hang a job on a call that never fully connected.
    """
    try:
        return await asyncio.wait_for(
            ctx.wait_for_participant(kind=rtc.ParticipantKind.PARTICIPANT_KIND_SIP),
            timeout=timeout,
        )
    except TimeoutError:
        return None


def _caller_info_from_sip_participant(
    participant: rtc.RemoteParticipant,
) -> CallerInfo:
    attrs = participant.attributes or {}
    return CallerInfo(
        call_id=attrs.get("sip.callID"),
        caller_from=attrs.get("sip.from") or _caller_from_identity(participant.identity),
        dialed_number=attrs.get("sip.trunkPhoneNumber"),
        source="sip",
    )


def _caller_from_identity(identity: str | None) -> str | None:
    """Extract a caller number from LiveKit SIP participant identity."""
    if not identity or not identity.startswith("sip_"):
        return None
    caller = identity.removeprefix("sip_").strip()
    if caller.startswith("+") and caller[1:].isdigit():
        return caller
    return None


async def resolve_caller(
    ctx: JobContext,
    *,
    sip_wait_seconds: float = DEFAULT_SIP_WAIT_SECONDS,
) -> CallerInfo:
    """Resolve caller info from the SIP participant on ``ctx.room``.

    Falls back to an all-``None`` ``CallerInfo`` with ``source="unknown"``
    if no SIP participant appears within ``sip_wait_seconds``.

    Call AFTER ``await ctx.connect()``.
    """
    participant = await _wait_for_sip_participant(ctx, sip_wait_seconds)
    if participant is not None:
        return _caller_info_from_sip_participant(participant)

    logger.warning(
        "resolve_caller: no SIP participant within %.1fs (room=%s metadata=%r)",
        sip_wait_seconds,
        ctx.room.name,
        ctx.job.metadata,
    )
    return CallerInfo(
        call_id=None,
        caller_from=None,
        dialed_number=None,
        source="unknown",
    )
