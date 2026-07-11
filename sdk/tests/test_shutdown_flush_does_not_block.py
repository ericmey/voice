"""The `-10` bug: a synchronous OTel flush on the event loop gets the job process killed.

## What was wrong

`wire_otel_shutdown_flush` registered this as a LiveKit shutdown callback::

    async def _flush_otel(_reason: str = "") -> None:
        force_flush_otel_tracing(timeout_millis)   # sync. no await. no thread.

`async def` makes it *look* like a coroutine, which is why it read as correct for months.
But `force_flush_otel_tracing` performs a **synchronous network export** to the OTel
collector on shiori — OTel's `force_flush` blocks the calling thread on a condition
variable until the exporter drains or the deadline expires.

Blocking the loop inside a shutdown callback starves every sibling callback LiveKit gathers
concurrently (audio finalize, Musubi close, `delete_room`) **and** the IPC read/ping tasks —
so the child can no longer answer its parent. The parent waits
`shutdown_process_timeout` (10.0s), then sends SIGUSR1. **Exit `-10` IS SIGUSR1.** The
process was shot; it did not crash.

Two things made it deterministic rather than occasional:

1. the flush ceiling (10 000 ms) was *numerically identical* to the parent's kill budget;
2. the timeout was applied **per provider** (tracing, logs, metrics) rather than as a
   total — so a 10 s request could block for **30 s**.

## What these tests prove

The load-bearing one is `test_the_event_loop_stays_responsive_while_the_flush_wedges`. It
wedges the flush and asserts the loop keeps running anyway — because "the loop keeps
running" is the *entire* fix. If the loop lives, IPC answers, the job exits clean, and the
kill never fires.

A test that only checked "flush was called" would have passed against the broken code.
"""

from __future__ import annotations

import asyncio
import threading
import time
from collections.abc import Awaitable, Callable

import pytest

from sdk import tracing


class _Ctx:
    """Minimal stand-in for LiveKit's JobContext — captures the shutdown callback."""

    def __init__(self) -> None:
        self.callback: Callable[[str], Awaitable[None]] = _unset_callback

    def add_shutdown_callback(self, fn: Callable[[str], Awaitable[None]]) -> None:
        self.callback = fn


async def _unset_callback(_reason: str = "") -> None:  # pragma: no cover
    raise AssertionError("wire_otel_shutdown_flush never registered a shutdown callback")


@pytest.fixture(autouse=True)
def _restore_providers():
    """Every test here fakes the providers; put them back afterwards."""
    saved = (tracing._provider, tracing._logger_provider, tracing._meter_provider)
    yield
    tracing._provider, tracing._logger_provider, tracing._meter_provider = saved


class _Provider:
    """A provider whose force_flush blocks for `blocks_for` seconds, like the real one."""

    def __init__(self, blocks_for: float = 0.0) -> None:
        self.blocks_for = blocks_for
        self.timeouts_seen: list[int] = []

    def force_flush(self, timeout_millis: int) -> bool:
        self.timeouts_seen.append(timeout_millis)
        if self.blocks_for:
            time.sleep(self.blocks_for)
        return True


# ---------------------------------------------------------------------------
# THE ONE THAT MATTERS
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_the_event_loop_stays_responsive_while_the_flush_wedges() -> None:
    """A wedged exporter must NOT stop the event loop. This is the whole `-10` fix.

    We wedge the flush far past its budget and run a heartbeat task alongside it. Against
    the old (synchronous) implementation the heartbeat cannot tick at all — the loop is
    blocked — which is exactly why LiveKit's IPC went unanswered and the parent killed the
    process. Against the fix, the heartbeat keeps ticking and the callback returns.
    """
    tracing._provider = _Provider(blocks_for=5.0)  # far beyond the 3s budget
    tracing._logger_provider = None
    tracing._meter_provider = None

    ctx = _Ctx()
    tracing.wire_otel_shutdown_flush(ctx, timeout_millis=300)
    assert ctx.callback is not None

    ticks = 0

    async def _heartbeat() -> None:
        """Stands in for LiveKit's IPC read/ping tasks — the things whose silence gets the
        process killed."""
        nonlocal ticks
        while True:
            await asyncio.sleep(0.01)
            ticks += 1

    beat = asyncio.create_task(_heartbeat())
    try:
        await asyncio.wait_for(ctx.callback("test"), timeout=5.0)
    finally:
        beat.cancel()

    assert ticks > 5, (
        f"the event loop only ticked {ticks} times while the flush was wedged — it was "
        f"BLOCKED. This is the -10 bug: LiveKit's IPC tasks cannot run, the child never "
        f"answers its parent, and the parent SIGUSR1s it at shutdown_process_timeout."
    )


@pytest.mark.asyncio
async def test_the_callback_returns_even_if_the_exporter_never_finishes() -> None:
    """A hung collector must not hold the job open. Abandon the flush; exit clean.

    Losing spans for one call is strictly better than being killed mid-teardown — the kill
    also loses the call's memory write.
    """
    tracing._provider = _Provider(blocks_for=30.0)
    tracing._logger_provider = None
    tracing._meter_provider = None

    ctx = _Ctx()
    tracing.wire_otel_shutdown_flush(ctx, timeout_millis=200)

    started = time.monotonic()
    await asyncio.wait_for(ctx.callback("test"), timeout=3.0)
    elapsed = time.monotonic() - started

    assert elapsed < 2.0, (
        f"callback took {elapsed:.1f}s against a 200ms budget — it is waiting on the "
        f"exporter instead of abandoning it."
    )


# ---------------------------------------------------------------------------
# The budget must be a TOTAL, and must stay under the kill deadline
# ---------------------------------------------------------------------------


def test_flush_timeout_is_a_total_budget_not_per_provider() -> None:
    """Three providers must SHARE the budget, not each receive it.

    The old code passed the full `timeout_millis` to each of tracing, logs and metrics — so
    a 10s request could block for 30s, against a 10s kill deadline.
    """
    trace_p = _Provider(blocks_for=0.15)
    logs_p = _Provider(blocks_for=0.15)
    metrics_p = _Provider(blocks_for=0.15)
    tracing._provider, tracing._logger_provider, tracing._meter_provider = (
        trace_p,
        logs_p,
        metrics_p,
    )

    started = time.monotonic()
    tracing.force_flush_otel_tracing(timeout_millis=400)
    elapsed = time.monotonic() - started

    assert elapsed < 0.9, (
        f"three providers took {elapsed:.2f}s against a 400ms TOTAL budget — the timeout is "
        f"being applied per-provider. Worst case is then 3x the requested ceiling."
    )

    # Each provider must see a SHRINKING remainder, never the full budget again.
    assert logs_p.timeouts_seen[0] < 400, (
        f"the logs provider was handed {logs_p.timeouts_seen[0]}ms — the full budget, after "
        f"tracing had already spent part of it. The budget is not being decremented."
    )


def test_flush_budget_cannot_exceed_the_kill_deadline() -> None:
    """The guard-rail. If someone raises FLUSH_BUDGET_MS to 10000 again, this fails.

    The original bug was a flush ceiling numerically identical to the parent's kill budget.
    The budget must stay comfortably under it — it is one of several concurrent shutdown
    callbacks, not the only one.
    """
    budget_s = tracing.FLUSH_BUDGET_MS / 1000.0
    assert budget_s < tracing.LIVEKIT_SHUTDOWN_BUDGET_S, (
        f"FLUSH_BUDGET_MS ({tracing.FLUSH_BUDGET_MS}ms) is not under LiveKit's "
        f"shutdown_process_timeout ({tracing.LIVEKIT_SHUTDOWN_BUDGET_S}s). This is how the "
        f"-10 bug happened."
    )
    assert budget_s <= tracing.LIVEKIT_SHUTDOWN_BUDGET_S / 2, (
        "the flush is ONE of several concurrent shutdown callbacks (audio finalize, Musubi "
        "close, delete_room). Taking more than half the total budget starves them."
    )


def test_no_providers_configured_is_a_clean_no_op() -> None:
    """OTel off (tests, CI, local dev) must not make shutdown slower or noisier."""
    tracing._provider = tracing._logger_provider = tracing._meter_provider = None
    assert tracing.force_flush_otel_tracing(timeout_millis=1000) is True


@pytest.mark.asyncio
async def test_the_flush_thread_is_a_daemon() -> None:
    """The abandonment has to be REAL. This is the bug the first draft of the fix shipped.

    `asyncio.to_thread` looks like the obvious tool, and the first version of this fix used
    it. But it runs on the loop's default ThreadPoolExecutor, whose threads are NON-daemon
    (CPython 3.9+) — and `concurrent.futures` registers an atexit hook that JOINS them at
    interpreter shutdown.

    So a wedged `to_thread` flush is not abandonable: `wait_for` returns, the coroutine
    finishes, LiveKit's teardown completes... and then the interpreter hangs at exit waiting
    on the same export. The process still overruns, still gets SIGUSR1'd, still exits -10.
    The bug simply moves to a line where nobody is looking for it.

    Nothing at the call site shows this. Only the thread's `daemon` flag does. So it gets a
    test.
    """
    seen: dict[str, object] = {}

    class _Introspect:
        def force_flush(self, timeout_millis: int) -> bool:
            seen["daemon"] = threading.current_thread().daemon
            seen["name"] = threading.current_thread().name
            return True

    tracing._provider = _Introspect()
    tracing._logger_provider = None
    tracing._meter_provider = None

    ctx = _Ctx()
    tracing.wire_otel_shutdown_flush(ctx, timeout_millis=500)
    await ctx.callback("test")

    assert seen.get("daemon") is True, (
        "the OTel flush runs on a NON-daemon thread. The interpreter joins those at exit, so "
        "abandoning a wedged flush would not actually abandon it — the process would hang at "
        "shutdown on the same export and still be killed. Use an explicit daemon thread, not "
        "asyncio.to_thread."
    )
