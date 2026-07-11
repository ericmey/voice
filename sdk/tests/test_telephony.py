"""Unit tests for SIP caller resolution."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest
from livekit import rtc
from sdk.telephony import (
    CallerInfo,
    resolve_caller,
)


def _make_ctx(metadata: str | None, participants: dict) -> MagicMock:
    """Build a fake JobContext sufficient for resolve_caller().

    `resolve_caller` now uses LiveKit's own `ctx.wait_for_participant(kind=...)` instead of a
    hand-rolled 50ms polling loop over `room.remote_participants`.

    The fake must behave like the REAL one, or these tests prove nothing. Two properties of
    the real API matter here, both read out of the shipped wheel rather than assumed:

    1. "If the participant has already joined, the function will return immediately." That is
       the normal case for SIP — livekit-sip creates the room and the caller joins BEFORE the
       agent is dispatched. A fake that only resolved on a future join would have passed while
       the real thing hung every call.
    2. It has NO timeout — it waits forever. `resolve_caller` bounds it with `asyncio.wait_for`
       so a call that never fully connects cannot hang the job. So the fake must also never
       return on its own when there is no match: the timeout is what has to fire.
    """
    ctx = MagicMock()
    ctx.job.metadata = metadata
    ctx.room.name = "test-room"
    ctx.room.remote_participants = participants

    async def _wait_for_participant(*, identity=None, kind=None):
        wanted = kind if isinstance(kind, list) else [kind]
        for p in participants.values():
            if p.kind in wanted:
                return p  # already joined -> immediate, like the real API
        await asyncio.Event().wait()  # no match -> block forever; the timeout must fire

    ctx.wait_for_participant = _wait_for_participant
    return ctx


def _sip_participant(
    *,
    call_id: str = "abc123@sip",
    caller_from: str = "+14155551234",
    dialed: str = "+14155559999",
    identity: str = "sip_+14155551234",
) -> SimpleNamespace:
    return SimpleNamespace(
        identity=identity,
        kind=rtc.ParticipantKind.PARTICIPANT_KIND_SIP,
        attributes={
            "sip.callID": call_id,
            "sip.from": caller_from,
            "sip.trunkPhoneNumber": dialed,
        },
    )


def _non_sip_participant() -> SimpleNamespace:
    # Agent participant, not SIP — should be ignored by the scan.
    return SimpleNamespace(
        kind=rtc.ParticipantKind.PARTICIPANT_KIND_AGENT,
        attributes={},
    )


@pytest.mark.asyncio
async def test_resolve_caller_returns_sip_info_when_participant_present() -> None:
    ctx = _make_ctx(
        metadata=None,
        participants={"p1": _sip_participant(call_id="SIP-ABC")},
    )
    info = await resolve_caller(ctx, sip_wait_seconds=0.5)
    assert info == CallerInfo(
        call_id="SIP-ABC",
        caller_from="+14155551234",
        dialed_number="+14155559999",
        source="sip",
    )


@pytest.mark.asyncio
async def test_resolve_caller_falls_back_to_sip_identity_for_caller_from() -> None:
    participant = _sip_participant(caller_from="", identity="sip_+13179957066")
    participant.attributes.pop("sip.from")
    ctx = _make_ctx(metadata=None, participants={"p1": participant})

    info = await resolve_caller(ctx, sip_wait_seconds=0.5)

    assert info.source == "sip"
    assert info.caller_from == "+13179957066"


@pytest.mark.asyncio
async def test_resolve_caller_ignores_non_number_sip_identity() -> None:
    participant = _sip_participant(caller_from="", identity="sip_test-call")
    participant.attributes.pop("sip.from")
    ctx = _make_ctx(metadata=None, participants={"p1": participant})

    info = await resolve_caller(ctx, sip_wait_seconds=0.5)

    assert info.source == "sip"
    assert info.caller_from is None


@pytest.mark.asyncio
async def test_resolve_caller_ignores_non_sip_participants() -> None:
    ctx = _make_ctx(
        metadata=None,
        participants={
            "agent1": _non_sip_participant(),
            "sip1": _sip_participant(call_id="SIP-XYZ"),
        },
    )
    info = await resolve_caller(ctx, sip_wait_seconds=0.5)
    assert info.source == "sip"
    assert info.call_id == "SIP-XYZ"


@pytest.mark.asyncio
async def test_resolve_caller_with_dispatch_metadata_present() -> None:
    """Dispatch rules may pack routing hints into ctx.job.metadata; that
    should have no effect on how we resolve the caller — attribute data
    on the SIP participant is authoritative."""
    ctx = _make_ctx(
        metadata='{"route": "vip", "source": "sip"}',
        participants={"p1": _sip_participant(call_id="SIP-DISP")},
    )
    info = await resolve_caller(ctx, sip_wait_seconds=0.5)
    assert info.source == "sip"
    assert info.call_id == "SIP-DISP"


@pytest.mark.asyncio
async def test_resolve_caller_unknown_when_nothing_resolves() -> None:
    ctx = _make_ctx(metadata=None, participants={})
    info = await resolve_caller(ctx, sip_wait_seconds=0.1)
    assert info.source == "unknown"
    assert info.call_id is None
    assert info.caller_from is None
    assert info.dialed_number is None


@pytest.mark.asyncio
async def test_resolve_caller_unknown_with_only_non_sip_participants() -> None:
    ctx = _make_ctx(
        metadata=None,
        participants={"agent1": _non_sip_participant()},
    )
    info = await resolve_caller(ctx, sip_wait_seconds=0.1)
    assert info.source == "unknown"
