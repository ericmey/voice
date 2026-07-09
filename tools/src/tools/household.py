"""HouseholdToolsMixin — cross-agent read tool.

Gives a curated set of agents (Nyla, Aoi) the ability to see what
*other* agents have been doing recently. The per-agent mixin
(``MusubiToolsMixin``) only surfaces the caller's own episodic
stream; this mixin fans out to the ``AgentConfig.household_presences``
list and merges the results into one time-ordered view.

Not mixed into every agent — only agents that are expected to survey
the house. Party/voice personas that mirror another agent should not
compose this mixin (their ``household_presences`` defaults to the
empty tuple and the tool would degrade anyway).
"""

from __future__ import annotations

import asyncio
import logging
import time
from typing import Any

import aiohttp
from livekit.agents import Agent, function_tool
from sdk.config import NYLA_DEFAULT_CONFIG, AgentConfig
from sdk.musubi_v2_client import (
    MusubiV2AuthError,
    MusubiV2Client,
    MusubiV2ClientConfig,
    MusubiV2ClientError,
    MusubiV2Error,
    MusubiV2ServerError,
    MusubiV2TimeoutError,
)
from sdk.trace import trace

from tools.memory import _format_row

logger = logging.getLogger("voice.agent")

_DEGRADED_TRANSIENT = "Couldn't check household status — Musubi is unavailable right now."
_DEGRADED_HARD = "Couldn't check household status — access denied or misconfigured."
_MAX_HOURS = 168  # one week — widest reasonable window for "what's been going on"
_MAX_LIMIT = 30
_PER_PRESENCE_SCROLL = 100  # how many rows per presence to over-fetch before merging
_MAX_PAGES_PER_PRESENCE = 3  # cap pagination to keep latency bounded


class HouseholdToolsMixin(Agent):
    """Provides ``household_status`` — aggregate recent activity
    across every configured presence in ``config.household_presences``.

    Reads:
      - ``self.config.household_presences`` — presences to survey. If
        empty, the tool returns a clear diagnostic rather than an
        empty result (so Eric knows the mixin is wired but the config
        isn't).
      - ``MUSUBI_V2_BASE_URL`` / ``MUSUBI_V2_TOKEN`` env. Token must
        grant ``eric/*/episodic:r`` scope — narrower tokens will get
        403s per-presence which are logged and skipped.
    """

    config: AgentConfig = NYLA_DEFAULT_CONFIG

    def _musubi_v2_client(self) -> MusubiV2Client:
        return MusubiV2Client(config=MusubiV2ClientConfig.from_env())

    async def _scroll_one_presence(
        self,
        client: MusubiV2Client,
        presence: str,
        cutoff: float,
        need: int,
        session: aiohttp.ClientSession,
    ) -> list[dict[str, Any]]:
        """Paginate ``GET /v1/episodic`` for a single presence until
        we have ``need`` rows newer than ``cutoff`` or pages/cursors
        are exhausted."""
        rows: list[dict[str, Any]] = []
        cursor: str | None = None
        namespace = f"{presence}/episodic"

        for _ in range(_MAX_PAGES_PER_PRESENCE):
            page = await client.list_episodic(
                namespace=namespace,
                limit=_PER_PRESENCE_SCROLL,
                cursor=cursor,
                session=session,
            )
            items = page.get("items") or []
            for r in items:
                if (r.get("created_epoch") or 0) >= cutoff:
                    rows.append(r)
            cursor = page.get("next_cursor")
            if not cursor:
                break

        return rows

    @function_tool
    async def household_status(self, hours: int = 24, limit: int = 15) -> str:
        """Survey recent memories across every agent in the household.

        Invocation Condition: Invoke when the user asks about activity
        *beyond* what you yourself have been doing — e.g. "What's
        everyone been up to?", "How's the house?", "Any updates from
        the other agents?". If the question is only about your own
        past, use ``musubi_recent`` instead.

        Args:
            hours: How many hours back to look across all agents
                (default 24, max 168).
            limit: Maximum merged rows to surface (default 15, max 30).
        """
        trace(f"tool=household_status hours={hours} limit={limit}")
        presences = self.config.household_presences
        if not presences:
            logger.warning("household_status: no household_presences configured")
            return "Household status isn't configured for this agent."

        hours = max(1, min(hours, _MAX_HOURS))
        limit = max(1, min(limit, _MAX_LIMIT))
        cutoff = time.time() - (hours * 3600)
        client = self._musubi_v2_client()

        timeout = aiohttp.ClientTimeout(total=client.config.timeout_s * 2)
        async with aiohttp.ClientSession(timeout=timeout) as session:
            coros = [
                self._scroll_one_presence(
                    client=client,
                    presence=presence,
                    cutoff=cutoff,
                    need=limit,
                    session=session,
                )
                for presence in presences
            ]
            settled = await asyncio.gather(*coros, return_exceptions=True)

        merged: list[dict[str, Any]] = []
        any_success = False
        any_transient = False
        for presence, result in zip(presences, settled, strict=True):
            if isinstance(result, (MusubiV2TimeoutError, MusubiV2ServerError)):
                logger.warning("household_status: transient for %s: %s", presence, result)
                any_transient = True
                continue
            if isinstance(result, MusubiV2AuthError):
                logger.warning("household_status: auth denied for %s: %s", presence, result)
                continue
            if isinstance(result, MusubiV2ClientError):
                logger.error("household_status: bad request for %s: %s", presence, result)
                continue
            if isinstance(result, MusubiV2Error):
                logger.warning("household_status: %s: %s", presence, result)
                continue
            if isinstance(result, BaseException):
                logger.warning("household_status: unexpected %s: %r", presence, result)
                continue
            any_success = True
            merged.extend(result)

        if not any_success:
            return _DEGRADED_TRANSIENT if any_transient else _DEGRADED_HARD

        if not merged:
            return "No recent activity across the household."

        merged.sort(key=lambda r: r.get("created_epoch") or 0, reverse=True)
        top = merged[:limit]
        return "\n\n".join(_format_row(r) for r in top)


__all__ = ["HouseholdToolsMixin"]
