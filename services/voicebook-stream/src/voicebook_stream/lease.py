"""One-flight lease with a concrete reservation whose release is idempotent and
independent of iteration.

Two traps, both verified directly:
  1. Lazy acquire: a generator-based guard acquires only on first iteration, by
     which point a StreamingResponse may already have sent 200 headers. So
     acquisition is SYNCHRONOUS, at the route, before the response is built.
  2. Unstarted-generator abandonment: closing a generator that was never
     started does NOT run its finally (confirmed). Starlette sends
     http.response.start before pulling the first body chunk; if that send
     fails, or cancellation lands before the first next(), a finally-in-the-body
     release never fires and the lease locks forever.

So release does NOT live in a generator finally. It lives in a Reservation with
an idempotent close() that the ROUTE calls from a response-level finally
covering response-start failure, completion, iteration error, and disconnect.
"""

from __future__ import annotations

import threading


class Busy(RuntimeError):
    """A generation is already in flight. Route maps this to 429."""


class Reservation:
    """Owns exactly one release. close() is idempotent and safe to call whether
    or not any streaming ever started."""

    def __init__(self, lock: threading.Lock) -> None:
        self._lock = lock
        self._released = False
        self._guard = threading.Lock()  # makes close() race-safe + idempotent

    def close(self) -> None:
        with self._guard:
            if self._released:
                return
            self._released = True
            self._lock.release()

    @property
    def released(self) -> bool:
        return self._released


class OneFlightLease:
    def __init__(self) -> None:
        self._lock = threading.Lock()

    def reserve(self) -> Reservation:
        """Synchronous, non-blocking. Call BEFORE constructing the response so a
        collision is a clean 429, never a half-sent 200. Returns a Reservation
        the caller MUST close() from a response-level finally — never from a
        body-generator finally, which an unstarted generator skips."""
        if not self._lock.acquire(blocking=False):
            raise Busy()
        return Reservation(self._lock)

    @property
    def locked(self) -> bool:
        return self._lock.locked()
