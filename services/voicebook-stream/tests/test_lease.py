"""Reservation-based one-flight lease. No GPU."""

from __future__ import annotations

import threading

import pytest
from voicebook_stream.lease import Busy, OneFlightLease


def test_reserve_is_eager_and_second_reserve_is_busy():
    lease = OneFlightLease()
    r = lease.reserve()
    with pytest.raises(Busy):
        lease.reserve()  # synchronous refusal, no iteration involved
    r.close()
    lease.reserve().close()  # free again


def test_close_releases():
    lease = OneFlightLease()
    r = lease.reserve()
    assert lease.locked is True
    r.close()
    assert lease.locked is False


def test_close_is_idempotent():
    lease = OneFlightLease()
    r = lease.reserve()
    r.close()
    r.close()  # must not release a second time / raise
    r.close()
    # lock is free and healthy — a fresh reserve works, and its own double-close
    # does not corrupt the lock
    r2 = lease.reserve()
    r2.close()
    r2.close()
    assert lease.locked is False


def test_release_without_ever_starting_a_stream():
    """The pre-first-iteration leak: a reservation is taken, NO streaming ever
    happens (response-start failed / cancelled before first chunk), and close()
    from a response-level finally must still release. A body-generator finally
    would NOT run here."""
    lease = OneFlightLease()
    r = lease.reserve()
    # ... no iterator ever created or started ...
    r.close()  # response-level finally
    assert lease.locked is False
    lease.reserve().close()  # proves it is genuinely free


def test_reservation_is_threadsafe_under_concurrent_close():
    lease = OneFlightLease()
    r = lease.reserve()
    errors: list = []

    def closer():
        try:
            r.close()
        except Exception as e:  # noqa: BLE001
            errors.append(e)

    ts = [threading.Thread(target=closer) for _ in range(8)]
    for t in ts:
        t.start()
    for t in ts:
        t.join()
    assert errors == []
    assert lease.locked is False
