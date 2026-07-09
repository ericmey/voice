"""Live-target smoke suite — runs the v2 client + canonical Musubi
tools mixin against a real Musubi server. Skipped by default so
`make test` stays hermetic.

To run:

    MUSUBI_LIVE_BASE_URL=http://localhost:8100/v1 \
    MUSUBI_LIVE_TOKEN=<harness-bearer> \
    MUSUBI_LIVE_NS_ROOT=harness/v2-smoke \
    uv run pytest sdk/tests/test_musubi_live_smoke.py -v

The bearer must carry scope for `<ns-root>/*:rw` plus `thoughts:send`.
See the Musubi repo's `tests/integration/conftest.py` for the token
shape or the ad-hoc JWT in `/tmp/musubi-harness-token`.

Every test writes into the harness namespace and either round-trips
or asserts the wire-level envelope. Repeat runs are safe: the
episodic plane dedups identical content within a namespace, and the
harness NS prefix is isolated from production data by the token
scope.
"""

from __future__ import annotations

import asyncio
import os
import uuid

import pytest
from sdk.config import AgentConfig
from sdk.musubi_v2_client import (
    MusubiV2AuthError,
    MusubiV2Client,
    MusubiV2ClientConfig,
)

pytestmark = pytest.mark.asyncio

_BASE_URL = os.environ.get("MUSUBI_LIVE_BASE_URL")
_TOKEN = os.environ.get("MUSUBI_LIVE_TOKEN")
_NS_ROOT = os.environ.get("MUSUBI_LIVE_NS_ROOT", "harness/v2-smoke")

_live_enabled = bool(_BASE_URL) and bool(_TOKEN)
_skip_reason = "set MUSUBI_LIVE_BASE_URL + MUSUBI_LIVE_TOKEN to run"

skip_unless_live = pytest.mark.skipif(not _live_enabled, reason=_skip_reason)


def _live_client() -> MusubiV2Client:
    return MusubiV2Client(
        config=MusubiV2ClientConfig(
            base_url=_BASE_URL or "",
            token=_TOKEN or "",
            timeout_s=10.0,
        ),
    )


@skip_unless_live
async def test_live_capture_memory_roundtrip() -> None:
    """v2 client capture_memory round-trips against the live box and
    returns the canonical ack envelope."""
    client = _live_client()
    marker = f"capture-live-{uuid.uuid4().hex[:8]}"

    ack = await client.capture_memory(
        namespace=f"{_NS_ROOT}/episodic",
        content=f"live-smoke capture probe {marker}",
        tags=["live-smoke", f"ref:{marker}"],
        importance=5,
        idempotency_key=f"live-smoke:{marker}",
    )
    assert "object_id" in ack
    assert ack.get("state") == "provisional"


@skip_unless_live
async def test_live_retrieve_runs_clean_on_empty_ns() -> None:
    """Retrieve returns a shaped response (even on zero hits)."""
    client = _live_client()
    resp = await client.retrieve(
        namespace=f"{_NS_ROOT}/episodic",
        query_text=f"nothing-matches-{uuid.uuid4().hex}",
        mode="fast",
        limit=3,
    )
    assert "results" in resp
    assert resp.get("mode") == "fast"


@skip_unless_live
async def test_live_send_thought_accepts_three_segment_namespace() -> None:
    """send_thought on a 3-segment `<root>/thought` namespace returns
    the canonical `{object_id, state}` envelope."""
    client = _live_client()
    marker = f"thought-live-{uuid.uuid4().hex[:8]}"

    ack = await client.send_thought(
        namespace=f"{_NS_ROOT}/thought",
        from_presence=_NS_ROOT,
        to_presence="nyla",
        content=f"live-smoke thought {marker}",
    )
    assert "object_id" in ack


@skip_unless_live
async def test_live_scope_violation_raises_auth_error() -> None:
    """Writing to a namespace outside the token's scope must surface
    as `MusubiV2AuthError` so the voice tool's catch path can degrade
    gracefully rather than blowing up the turn."""
    client = _live_client()
    with pytest.raises(MusubiV2AuthError):
        await client.capture_memory(
            namespace="eric/claude-code/episodic",
            content="should-be-403",
            importance=1,
        )


@skip_unless_live
async def test_live_canonical_search_against_wildcard_namespace() -> None:
    """`musubi_search` (canonical surface) must round-trip a real
    retrieve against the live server using the tenant-wildcard
    episodic namespace and surface a clean envelope on zero hits.

    Replaces the legacy three-plane fan-out smoke — the canonical
    surface uses a single retrieve against `<tenant>/*/episodic`
    rather than per-plane fan-out (Musubi ADR 0031 + ADR 0032)."""
    # Import lazily so the module is skippable for non-live runs
    # even when the mixin's livekit deps aren't installed.
    from tools.memory import MusubiToolsMixin  # noqa: PLC0415

    cfg = AgentConfig(
        agent_name="smoke",
        memory_agent_tag="smoke-voice",
        musubi_v2_namespace=_NS_ROOT,
    )

    inst = MusubiToolsMixin.__new__(MusubiToolsMixin)
    inst.config = cfg  # type: ignore[misc]
    inst._musubi_v2_client = lambda: _live_client()  # type: ignore[method-assign]

    # Bypass the @function_tool descriptor to drive the coroutine
    # directly (same pattern as the unit tests).
    search = MusubiToolsMixin.musubi_search.__wrapped__  # type: ignore[attr-defined]
    result = await search(inst, query=f"nothing-{uuid.uuid4().hex}", limit=3)

    # "No memories matched." is the success envelope for an empty
    # search; a degraded-mode message would indicate an auth or
    # transport problem on the live target.
    assert "Couldn't reach memory" not in result


if __name__ == "__main__":
    # Manual smoke runner — prints a compact summary. Useful for
    # wiring the harness into cutover-day checklists without pytest.
    async def _main() -> None:
        if not _live_enabled:
            print("set MUSUBI_LIVE_BASE_URL + MUSUBI_LIVE_TOKEN")
            return
        client = _live_client()
        marker = uuid.uuid4().hex[:6]
        ack = await client.capture_memory(
            namespace=f"{_NS_ROOT}/episodic",
            content=f"manual-smoke {marker}",
            importance=3,
        )
        print(f"capture ok — object_id={ack.get('object_id')}")
        resp = await client.retrieve(
            namespace=f"{_NS_ROOT}/episodic",
            query_text=marker,
            mode="fast",
            limit=1,
        )
        print(f"retrieve ok — {len(resp.get('results') or [])} hits")

    asyncio.run(_main())
