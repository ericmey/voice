"""Tests for HouseholdToolsMixin — household_status.

Behavioral tests monkeypatch ``_musubi_v2_client()`` via a tiny fake
so no real HTTP flies.
"""

from __future__ import annotations

import asyncio
import time
from typing import Any

from sdk.config import AgentConfig
from tools.household import HouseholdToolsMixin


def _run(coro: Any) -> Any:
    """Drive a coroutine from a sync test.

    Was ``asyncio.get_event_loop().run_until_complete(coro)``, which only worked
    when some earlier async test happened to leave a loop installed on the main
    thread. On 3.12 that raises "There is no current event loop" the moment
    collection order changes. It passed by accident, not by design.
    """
    return asyncio.run(coro)


class _FakeConfig:
    timeout_s = 1.0


class _FakeV2Client:
    """Records calls and returns canned responses; stands in for
    ``MusubiV2Client`` via the ``_musubi_v2_client`` override point."""

    config = _FakeConfig()

    def __init__(
        self,
        pages: dict[str, list[dict[str, Any]]] | None = None,
        raises: Exception | None = None,
    ) -> None:
        # namespace -> list of page dicts (each page = {"items": [...], "next_cursor": ...})
        self._pages = pages or {}
        self._raises = raises
        self.calls: list[dict[str, Any]] = []

    async def list_episodic(
        self,
        *,
        namespace: str,
        limit: int = 50,
        cursor: str | None = None,
        session: Any | None = None,
    ) -> dict[str, Any]:
        self.calls.append({"namespace": namespace, "limit": limit, "cursor": cursor})
        if self._raises:
            raise self._raises
        key_pages = self._pages.get(namespace, [])
        # Find the page matching the cursor (None = first page)
        for _i, page in enumerate(key_pages):
            page_cursor = page.get("_cursor")
            if cursor == page_cursor:
                return {"items": page["items"], "next_cursor": page.get("next_cursor")}
        # Default: return first page if no cursor match
        if key_pages:
            first = key_pages[0]
            return {"items": first["items"], "next_cursor": first.get("next_cursor")}
        return {"items": [], "next_cursor": None}


def _agent_cfg(presences: tuple[str, ...]) -> AgentConfig:
    return AgentConfig(
        agent_name="nyla",
        memory_agent_tag="nyla-voice",
        household_presences=presences,
    )


class _TestAgent:
    """Minimal concrete agent for testing — avoids ``__new__`` tricks."""

    config = _agent_cfg(("eric/nyla", "eric/aoi"))
    _fake: Any = None

    def _musubi_v2_client(self) -> Any:
        return self._fake

    # Pull in the implementation we actually want to test.
    household_status = HouseholdToolsMixin.household_status
    _scroll_one_presence = HouseholdToolsMixin._scroll_one_presence


def _make_test_agent(fake: Any, presences: tuple[str, ...] | None = None) -> Any:
    inst = _TestAgent()
    inst._fake = fake
    if presences is not None:
        inst.config = _agent_cfg(presences)
    return inst


# ---------------------------------------------------------------------------
# Structural
# ---------------------------------------------------------------------------


def test_mixin_has_household_status() -> None:
    assert hasattr(HouseholdToolsMixin, "household_status")
    assert callable(HouseholdToolsMixin.household_status)


def test_empty_presences_returns_diagnostic() -> None:
    fake = _FakeV2Client()
    inst = _make_test_agent(fake)
    inst.config = _agent_cfg(())
    out = _run(inst.household_status())
    assert "isn't configured" in out
    assert fake.calls == []


# ---------------------------------------------------------------------------
# Behavioral — merge + sort + cutoff
# ---------------------------------------------------------------------------


def test_merges_and_sorts_across_presences() -> None:
    now = time.time()
    fake = _FakeV2Client(
        pages={
            "eric/nyla/episodic": [
                {
                    "_cursor": None,
                    "items": [
                        {
                            "content": "nyla old",
                            "created_epoch": now - 7200,
                            "tags": ["nyla-voice"],
                        },
                        {
                            "content": "nyla new",
                            "created_epoch": now - 1800,
                            "tags": ["nyla-voice"],
                        },
                    ],
                    "next_cursor": None,
                }
            ],
            "eric/aoi/episodic": [
                {
                    "_cursor": None,
                    "items": [
                        {"content": "aoi mid", "created_epoch": now - 3600, "tags": ["aoi-voice"]},
                    ],
                    "next_cursor": None,
                }
            ],
        }
    )
    inst = _make_test_agent(fake)
    # hours=2 => cutoff = now - 7200; nyla-old is exactly at cutoff, excluded
    out = _run(inst.household_status(hours=2, limit=10))
    lines = out.split("\n\n")
    assert len(lines) == 2
    assert "nyla new" in lines[0]  # newest first
    assert "aoi mid" in lines[1]


def test_respects_limit_after_merge() -> None:
    now = time.time()
    fake = _FakeV2Client(
        pages={
            "eric/nyla/episodic": [
                {
                    "_cursor": None,
                    "items": [
                        {"content": "nyla 1", "created_epoch": now - 100, "tags": ["nyla-voice"]},
                    ],
                    "next_cursor": None,
                }
            ],
            "eric/aoi/episodic": [
                {
                    "_cursor": None,
                    "items": [
                        {"content": "aoi 1", "created_epoch": now - 200, "tags": ["aoi-voice"]},
                        {"content": "aoi 2", "created_epoch": now - 50, "tags": ["aoi-voice"]},
                    ],
                    "next_cursor": None,
                }
            ],
        }
    )
    inst = _make_test_agent(fake)
    out = _run(inst.household_status(hours=1, limit=2))
    lines = out.split("\n\n")
    assert len(lines) == 2
    assert "aoi 2" in lines[0]
    assert "nyla 1" in lines[1]


def test_filters_by_cutoff() -> None:
    now = time.time()
    fake = _FakeV2Client(
        pages={
            "eric/nyla/episodic": [
                {
                    "_cursor": None,
                    "items": [
                        {
                            "content": "too old",
                            "created_epoch": now - 100_000,
                            "tags": ["nyla-voice"],
                        },
                        {"content": "recent", "created_epoch": now - 100, "tags": ["nyla-voice"]},
                    ],
                    "next_cursor": None,
                }
            ],
        }
    )
    inst = _make_test_agent(fake)
    inst.config = _agent_cfg(("eric/nyla",))
    out = _run(inst.household_status(hours=1, limit=10))
    assert "recent" in out
    assert "too old" not in out


# ---------------------------------------------------------------------------
# Error handling — partial failures + all failures
# ---------------------------------------------------------------------------


def test_skips_auth_denied_presence_survives_others() -> None:
    from sdk.musubi_v2_client import MusubiV2AuthError

    now = time.time()

    class _PartialFake:
        config = _FakeConfig()

        def __init__(self) -> None:
            self.calls: list[dict[str, Any]] = []

        async def list_episodic(self, *, namespace: str, **_: Any) -> dict[str, Any]:
            self.calls.append({"namespace": namespace})
            if "aoi" in namespace:
                raise MusubiV2AuthError("403 out of scope")
            return {
                "items": [
                    {"content": "nyla stuff", "created_epoch": now - 100, "tags": ["nyla-voice"]}
                ],
                "next_cursor": None,
            }

    fake = _PartialFake()
    inst = _make_test_agent(fake)
    out = _run(inst.household_status(hours=1, limit=10))
    assert "nyla stuff" in out
    assert len(fake.calls) == 2


def test_all_transient_returns_distinct_message() -> None:
    from sdk.musubi_v2_client import MusubiV2ServerError

    class _AllFail:
        config = _FakeConfig()

        async def list_episodic(self, *, namespace: str, **_: Any) -> dict[str, Any]:
            raise MusubiV2ServerError("503")

    inst = _make_test_agent(_AllFail())
    out = _run(inst.household_status())
    assert "unavailable right now" in out


def test_all_hard_fail_returns_distinct_message() -> None:
    from sdk.musubi_v2_client import MusubiV2AuthError

    class _AllFail:
        config = _FakeConfig()

        async def list_episodic(self, *, namespace: str, **_: Any) -> dict[str, Any]:
            raise MusubiV2AuthError("403")

    inst = _make_test_agent(_AllFail())
    out = _run(inst.household_status())
    assert "access denied or misconfigured" in out


# ---------------------------------------------------------------------------
# Pagination — follows next_cursor per presence
# ---------------------------------------------------------------------------


def test_paginates_until_need_met() -> None:
    now = time.time()
    fake = _FakeV2Client(
        pages={
            "eric/nyla/episodic": [
                {
                    "_cursor": None,
                    # First page has no rows passing the 2-hour cutoff,
                    # so pagination must continue to the second page.
                    "items": [
                        {"content": "page1", "created_epoch": now - 10000, "tags": ["nyla-voice"]},
                    ],
                    "next_cursor": "c1",
                },
                {
                    "_cursor": "c1",
                    "items": [
                        {"content": "page2", "created_epoch": now - 100, "tags": ["nyla-voice"]},
                    ],
                    "next_cursor": None,
                },
            ],
        }
    )
    inst = _make_test_agent(fake)
    inst.config = _agent_cfg(("eric/nyla",))
    out = _run(inst.household_status(hours=2, limit=1))
    assert "page2" in out
    assert "page1" not in out
    # Should have made two calls (first page + cursor follow)
    assert len(fake.calls) == 2
    assert fake.calls[1]["cursor"] == "c1"
