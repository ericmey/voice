"""MusubiToolsMixin — canonical agent-tools surface for voice agents.

Implements the five-tool canonical surface from Musubi
[[07-interfaces/agent-tools]] / ADR 0032: ``musubi_recent``,
``musubi_search``, ``musubi_get``, ``musubi_remember``, ``musubi_think``.
Identical names + parameter shapes across every Musubi adapter (this
mixin, the openclaw-musubi browser plugin, the Python MCP adapter)
so Eric (or any model) gets the same tool surface regardless of which
modality Aoi/Nyla is on.

``musubi_get`` ships in this mixin as a clearly-deferred stub — the
Python MusubiV2Client doesn't expose per-plane ``get(object_id)``
methods today, and SDK extensions are out of this slice's scope (see
[[_slices/slice-livekit-canonical-tools]] § Forbidden paths). The stub
returns a user-readable deferred message so a curious model + the
operator both see the dependency immediately.

``MemoryToolsMixin`` is preserved as a one-release deprecation alias
(``MemoryToolsMixin = MusubiToolsMixin`` at the bottom of this file)
so existing imports keep working through the deprecation window. Drops
in the next minor release.
"""

from __future__ import annotations

import logging
import uuid
from typing import Any

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

logger = logging.getLogger("openclaw-livekit.agent")

_DEGRADED_LOOKUP = "Couldn't check memory — Musubi is unavailable right now."
_DEGRADED_STORE = "Memory didn't save — Musubi is unavailable right now."
_MAX_RECENT_LIMIT = 20
_MAX_SEARCH_LIMIT = 10
_SCROLL_MULTIPLIER = 5
_DEFAULT_IMPORTANCE = 7
# Pages of GET /v1/episodic to walk while gathering callout-worthy rows.
# Bounded so a token-mismatch on this agent's tag doesn't spiral into
# scrolling the whole namespace.
_MAX_RECENT_PAGES = 5

# Search-side state filter. Default Musubi retrieve hides `provisional` so
# unscored ambient captures don't pollute results, but a deliberate
# `musubi_remember` from another channel sits as `provisional` until the
# hourly maturation cron runs. For explicit recall we want fresh
# deliberate stores visible immediately — opt into provisional alongside
# the default `(matured, promoted)`. Per Musubi v1.2.0 / state_filter API.
_SEARCH_STATE_FILTER = ["provisional", "matured", "promoted"]


class MusubiToolsMixin(Agent):
    """Provides the canonical agent-tools surface.

    Five tools per [[07-interfaces/agent-tools]] / ADR 0032:
    ``musubi_recent``, ``musubi_search``, ``musubi_get`` (deferred
    stub), ``musubi_remember``, ``musubi_think``.

    Per-agent scope: each agent reads/writes its own
    ``<agent>/<channel>/episodic`` per ADR 0030. Cross-channel reads
    (``musubi_search``) fan via ``<agent>/*/episodic``. Cross-agent
    surveying lives in ``HouseholdToolsMixin``.

    Reads:
      - ``self.config.musubi_v2_namespace`` (or ``musubi_v2_presence``
        for back-compat) — resolves the 3-segment namespace
        ``<agent>/<channel>/<plane>``. Falls back to
        ``eric/<agent_name>`` when unset (legacy human-as-tenant
        compatibility; live configs use the canonical agent-as-tenant
        form).
      - ``MUSUBI_V2_BASE_URL`` / ``MUSUBI_V2_TOKEN`` env.
    """

    config: AgentConfig = NYLA_DEFAULT_CONFIG

    def _musubi_v2_client(self) -> MusubiV2Client:
        """One place to construct the client so tests can monkeypatch."""
        return MusubiV2Client(config=MusubiV2ClientConfig.from_env())

    def _own_episodic_namespace(self) -> str | None:
        """Resolve this agent's own episodic namespace.

        Uses ``config.musubi_v2_namespace`` (documented as the
        namespace-scoping field) and appends ``/episodic``. Falls
        back to ``config.musubi_v2_presence`` for backward compat
        with configs that set presence but not namespace, then to
        ``eric/<agent_name>/episodic``.

        Returns ``None`` when the config prefix is malformed (not
        2-segment), matching ``MusubiVoiceToolsMixin._ns()``
        degradation behavior.
        """
        prefix = self.config.musubi_v2_namespace or self.config.musubi_v2_presence
        if not prefix:
            prefix = f"eric/{self.config.agent_name}"
        segments = prefix.split("/")
        if len(segments) != 2:
            logger.warning(
                "musubi_v2_namespace/presence %r is not 2-segment; episodic namespace will degrade",
                prefix,
            )
            return None
        return f"{prefix}/episodic"

    def _own_thought_namespace(self) -> str | None:
        """Resolve this agent's own thought namespace for ``musubi_think`` sends.

        Same prefix-resolution path as :meth:`_own_episodic_namespace`,
        but the trailing plane is ``thought`` instead of ``episodic``.
        Returns ``None`` when the prefix is malformed (degrades the
        tool to a friendly error).
        """
        prefix = self.config.musubi_v2_namespace or self.config.musubi_v2_presence
        if not prefix:
            prefix = f"eric/{self.config.agent_name}"
        segments = prefix.split("/")
        if len(segments) != 2:
            logger.warning(
                "musubi_v2_namespace/presence %r is not 2-segment; thought namespace will degrade",
                prefix,
            )
            return None
        return f"{prefix}/thought"

    def _own_presence(self) -> str:
        """Resolve a ``<agent>/<channel>`` presence string for ``from_presence`` claims.

        Prefers the explicit 2-segment namespace prefix; falls back to
        ``eric/<agent_name>`` legacy form when unset (matches the
        episodic-namespace fallback so behaviour is consistent across
        the mixin).
        """
        prefix = self.config.musubi_v2_namespace or self.config.musubi_v2_presence
        if not prefix:
            prefix = f"eric/{self.config.agent_name}"
        return prefix

    def _tenant_wildcard_episodic_namespace(self) -> str | None:
        """Resolve a tenant-wide wildcard namespace for cross-channel search.

        Per Musubi ADR 0031: ``<tenant>/*/episodic`` fans an episodic
        retrieve across every channel the tenant has captured into. For
        Nyla on the voice channel that means voice + openclaw + discord
        + any future surface — all read in one call. The agent still
        knows where each row came from because every result row carries
        its concrete stored namespace.

        Returns ``None`` when the agent's namespace is malformed (same
        degradation path as :meth:`_own_episodic_namespace`).
        """
        prefix = self.config.musubi_v2_namespace or self.config.musubi_v2_presence
        if not prefix:
            prefix = f"eric/{self.config.agent_name}"
        segments = prefix.split("/")
        if len(segments) != 2:
            logger.warning(
                "musubi_v2_namespace/presence %r is not 2-segment; tenant-wide search will degrade",
                prefix,
            )
            return None
        tenant = segments[0]
        return f"{tenant}/*/episodic"

    async def _scroll_episodic_recent(
        self,
        namespace: str,
        need: int,
        *,
        required_tag: str | None = None,
        max_pages: int = _MAX_RECENT_PAGES,
    ) -> list[dict[str, Any]]:
        """Paginate ``GET /v1/episodic`` until we have ``need`` recent
        rows or pages/cursors are exhausted. Recency-ordered (newest
        first); no time-window filter — "the last N memories" matters
        more than "memories from the last N hours" for greeting context.

        ``required_tag`` filters to rows whose ``tags`` list contains
        the value. Used to keep the greeting hook biased toward
        deliberate ``musubi_remember`` saves (which carry the agent's
        ``memory_agent_tag``) and away from ambient or operationally
        injected rows that lack it.
        """
        rows: list[dict[str, Any]] = []
        cursor: str | None = None
        client = self._musubi_v2_client()
        page_size = min(need * _SCROLL_MULTIPLIER, 500)

        for _ in range(max_pages):
            page = await client.list_episodic(
                namespace=namespace,
                limit=page_size,
                cursor=cursor,
            )
            items = page.get("items") or []
            for r in items:
                if required_tag is not None and required_tag not in (r.get("tags") or []):
                    continue
                rows.append(r)
            cursor = page.get("next_cursor")
            if not cursor or len(rows) >= need * _SCROLL_MULTIPLIER:
                break

        rows.sort(key=lambda r: r.get("created_epoch") or 0, reverse=True)
        return rows[:need]

    async def fetch_recent_context(self, limit: int = 10) -> str:
        """Plain-async fetch of recent voice memories for this agent.

        Exposed without ``@function_tool`` so ``on_enter`` can prefetch
        deterministically before the LLM gets a chance to skip the tool.

        Recency-based — returns the last ``limit`` rows tagged by this
        agent's voice mixin (``memory_agent_tag``), regardless of when
        they were captured. Filtering by tag keeps operational injections
        (smoke tests, manual API writes, anything that didn't come from
        a deliberate ``musubi_remember`` call) out of the greeting hook
        — those would otherwise read as "I was just thinking about
        v1 cutover smoke." which is exactly the texture we want to avoid.
        """
        trace(f"fetch_recent_context limit={limit}")
        limit = max(1, min(limit, _MAX_RECENT_LIMIT))
        namespace = self._own_episodic_namespace()
        if namespace is None:
            return _DEGRADED_LOOKUP

        voice_tag = self.config.memory_agent_tag

        try:
            rows = await self._scroll_episodic_recent(namespace, limit, required_tag=voice_tag)
        except (MusubiV2TimeoutError, MusubiV2ServerError) as err:
            logger.warning("fetch_recent_context: transient %s", err)
            return _DEGRADED_LOOKUP
        except MusubiV2AuthError as err:
            logger.error("fetch_recent_context: auth failure: %s", err)
            return _DEGRADED_LOOKUP
        except MusubiV2ClientError as err:
            logger.error("fetch_recent_context: bad request: %s", err)
            return _DEGRADED_LOOKUP
        except MusubiV2Error as err:
            logger.warning("fetch_recent_context: %s", err)
            return _DEGRADED_LOOKUP

        if not rows:
            return "No recent memories found."

        return "\n\n".join(_format_row(r) for r in rows)

    @function_tool
    async def musubi_recent(self, limit: int = 10) -> str:
        """Fetch recent memories from your own episodic stream.

        Invocation Condition: Invoke this tool whenever the user asks
        about your own recent activity, what you talked about before,
        or what's been going on with you. Examples: "What did we talk
        about yesterday?", "What have you been up to?" You MUST call
        this tool before making any claims about past conversations.

        Start-of-call context is already injected into your instructions
        by the runtime — you don't need to call this tool just to greet.

        Returns the most recent ``limit`` rows tagged by this voice
        agent. Operational / ambient writes that lack the agent tag are
        filtered out — recency, not a time window.

        Args:
            limit: Maximum number of memories to return (default 10, max 20).
        """
        return await self.fetch_recent_context(limit=limit)

    @function_tool
    async def musubi_search(self, query: str, limit: int = 5) -> str:
        """Semantic-search your memory across every channel you've spoken on.

        Invocation Condition: Invoke this tool when the user asks about
        a SPECIFIC topic, fact, or event you might know about — anything
        that isn't just "what did we talk about recently". Examples:
        "Do you remember the prank we discussed?", "What do you know
        about the dentist appointment?", "Did I tell you about the
        Stable Diffusion update?", "What's the deploy plan you saved?"

        Unlike musubi_recent (which is your VOICE channel only, last
        24h), this searches across voice + Openclaw + Discord + every
        other surface you exist on. If the user told Openclaw-you to
        remember something, THIS is the tool that finds it on a phone
        call. Each result row carries its origin namespace so you can
        say "we talked about that on Openclaw" vs "on our last call".

        You MUST call this tool when answering recall questions. Saying
        "I remember…" without calling this tool is hallucination.

        Args:
            query: What you're searching for. Plain English; the server
                runs hybrid + rerank.
            limit: Max rows to return (default 5).
        """
        trace(f"tool=musubi_search query={query[:60]!r} limit={limit}")
        if not query.strip():
            return "Error: query is required."
        limit = max(1, min(limit, _MAX_SEARCH_LIMIT))

        namespace = self._tenant_wildcard_episodic_namespace()
        if namespace is None:
            return _DEGRADED_LOOKUP

        try:
            response = await self._musubi_v2_client().retrieve(
                namespace=namespace,
                query_text=query,
                mode="deep",
                limit=limit,
                state_filter=_SEARCH_STATE_FILTER,
            )
        except (MusubiV2TimeoutError, MusubiV2ServerError) as err:
            logger.warning("musubi_search: transient %s", err)
            return _DEGRADED_LOOKUP
        except MusubiV2AuthError as err:
            logger.error("musubi_search: auth failure: %s", err)
            return _DEGRADED_LOOKUP
        except MusubiV2ClientError as err:
            logger.error("musubi_search: bad request: %s", err)
            return _DEGRADED_LOOKUP
        except MusubiV2Error as err:
            logger.warning("musubi_search: %s", err)
            return _DEGRADED_LOOKUP

        rows = response.get("results") or []
        if not rows:
            return "No memories matched."
        return "\n\n".join(_format_search_row(r) for r in rows)

    @function_tool
    async def musubi_remember(
        self,
        content: str,
        topics: list[str] | None = None,
        importance: int = _DEFAULT_IMPORTANCE,
    ) -> str:
        """Store a memory to Musubi for future recall.

        Invocation Condition: Invoke this tool whenever the user asks you
        to remember something, save something for later, or make a note.
        Also invoke proactively at the end of calls to save important
        context. Examples: "Remember I have a dentist appointment Tuesday",
        "Save that for later", "Don't forget about the deploy". You MUST
        call this tool to store the memory. Saying you'll remember it
        without calling this tool means the memory is lost.

        Tool name + parameter shape match the openclaw-musubi plugin's
        ``musubi_remember`` so saves on either surface look the same in
        traces and to the model.

        Args:
            content: What to remember. Write it the way you'd want to
                read it next time — natural language, not raw data.
            topics: Optional keywords for retrieval (e.g. ['joke', 'eric',
                'deploy']). Keep them short and relevant.
            importance: 1-10. Default 7. Bump higher for things you don't
                want demoted; lower for ambient context.
        """
        trace(f"tool=musubi_remember content={content[:60]!r} topics={topics!r}")
        if not content.strip():
            return "Error: content is required."

        topic_list = list(topics or [])
        # The agent's ``memory_agent_tag`` (e.g. ``nyla-voice``) goes in
        # alongside the caller's topics — that's the signal
        # :func:`fetch_recent_context` keys on to filter the greeting
        # hook to deliberate-save rows only.
        speaker_tag = self.config.memory_agent_tag
        if speaker_tag and speaker_tag not in topic_list:
            topic_list.append(speaker_tag)

        importance = max(1, min(int(importance), 10))

        namespace = self._own_episodic_namespace()
        if namespace is None:
            return _DEGRADED_STORE
        idem = f"livekit-musubi-remember:{uuid.uuid4().hex}"

        try:
            ack = await self._musubi_v2_client().capture_memory(
                namespace=namespace,
                content=content,
                tags=topic_list,
                importance=importance,
                idempotency_key=idem,
            )
        except (MusubiV2TimeoutError, MusubiV2ServerError) as err:
            logger.warning("musubi_remember: transient %s", err)
            return _DEGRADED_STORE
        except MusubiV2AuthError as err:
            logger.error("musubi_remember: auth failure: %s", err)
            return "Memory didn't save — auth failed."
        except MusubiV2ClientError as err:
            logger.error("musubi_remember: bad request: %s", err)
            return "Memory didn't save — request rejected."
        except MusubiV2Error as err:
            logger.warning("musubi_remember: %s", err)
            return "Memory didn't save — unknown error."

        object_id = ack.get("object_id") or "<unknown>"
        trace(f"tool=musubi_remember DONE id={object_id}")
        return "Got it, stored."

    # ------------------------------------------------------------------
    # musubi_think — presence-to-presence message
    # ------------------------------------------------------------------

    async def think_impl(
        self,
        to_presence: str,
        content: str,
        channel: str = "default",
        importance: int = 5,
    ) -> str:
        """Plain-async body of ``musubi_think``. Extracted so tests can
        target it without going through the ``@function_tool`` descriptor."""
        trace(f"tool=musubi_think to={to_presence!r} content={content[:60]!r} channel={channel!r}")
        namespace = self._own_thought_namespace()
        if namespace is None:
            logger.debug("musubi_think: no v2 namespace configured; degrading")
            return _DEGRADED_LOOKUP
        if not to_presence.strip():
            return "Error: to_presence is required."
        if not content.strip():
            return "Error: content is required."

        from_presence = self._own_presence()
        # Bare ``<agent>`` aliases resolve to ``<agent>/<this-channel>``
        # (this adapter is the voice channel). Canonical agent-as-tenant
        # form per ADR 0030 — distinct from the pre-v1.0 musubi_voice.py
        # version which resolved to ``eric/<agent>``.
        own_channel = (
            self._own_presence().split("/", 1)[1] if "/" in self._own_presence() else "voice"
        )
        resolved_to = to_presence if "/" in to_presence else f"{to_presence}/{own_channel}"

        try:
            ack = await self._musubi_v2_client().send_thought(
                namespace=namespace,
                from_presence=from_presence,
                to_presence=resolved_to,
                content=content,
                channel=channel,
                importance=importance,
            )
        except (MusubiV2TimeoutError, MusubiV2ServerError) as err:
            logger.warning("musubi_think: transient %s", err)
            return "Thought didn't deliver — Musubi is unavailable."
        except MusubiV2AuthError as err:
            logger.error("musubi_think: auth failure: %s", err)
            return "Thought didn't deliver — auth failed."
        except MusubiV2ClientError as err:
            logger.error("musubi_think: bad request: %s", err)
            return "Thought didn't deliver — request rejected."
        except MusubiV2Error as err:
            logger.warning("musubi_think: %s", err)
            return "Thought didn't deliver — unknown error."

        object_id = ack.get("object_id") or "<unknown>"
        return f"Sent to {resolved_to}. (id={object_id})"

    @function_tool
    async def musubi_think(
        self,
        to_presence: str,
        content: str,
        channel: str = "default",
        importance: int = 5,
    ) -> str:
        """Send a presence-to-presence thought to another agent.

        Invocation Condition: Invoke this tool when the user asks you
        to tell another agent something. Examples: "Tell my Claude
        Code session the deploy is done", "Let Aoi know I'm heading
        out", "Send a note to Nyla". You MUST call this tool to
        actually deliver the message — saying it without calling means
        nothing was sent.

        Args:
            to_presence: Recipient. Either canonical ``<agent>/<channel>``
                form (``aoi/voice``, ``nyla/discord``) or a bare
                ``<agent>`` alias the adapter resolves to its own
                channel. ``all`` broadcasts.
            content: The thought to deliver. Short, natural. Recipient
                reads it as if you paged them.
            channel: Channel within the recipient's inbox. Defaults
                to ``default``; use ``scheduler`` for time-boxed
                reminders.
            importance: 1-10. Default 5.
        """
        return await self.think_impl(
            to_presence=to_presence,
            content=content,
            channel=channel,
            importance=importance,
        )

    # ------------------------------------------------------------------
    # musubi_get — deferred stub (depends on SDK plane.get extension)
    # ------------------------------------------------------------------

    @function_tool
    async def musubi_get(
        self,
        plane: str,
        namespace: str,
        object_id: str,
    ) -> str:
        """Fetch one Musubi object's full content + metadata by id.

        NOT YET WIRED in the voice mixin — depends on the Python
        ``MusubiV2Client`` gaining per-plane ``get(object_id)``
        accessors. Tracked separately; SDK extensions are out of
        scope for [[_slices/slice-livekit-canonical-tools]]. The
        tool registers so its name is reserved at the canonical
        surface; calls return a clear deferred message.

        Use ``musubi_search`` to find content; this tool will surface
        the full underlying object once the SDK extension lands.
        """
        trace(f"tool=musubi_get plane={plane!r} object_id={object_id!r}")
        # Per CLAUDE.md prohibited patterns: ADR-punted dependencies
        # fail loud. The tool's contract is canonical, but the SDK
        # extension it needs is on the way. We log at WARNING so the
        # operator notices repeated calls in degraded mode.
        logger.warning(
            "musubi_get invoked but is not yet available in the voice "
            "mixin (depends on MusubiV2Client per-plane .get() accessors)"
        )
        return (
            f"musubi_get is not yet available in the voice mixin — its SDK "
            f"dependency (per-plane MusubiV2Client.{plane}.get) hasn't shipped. "
            f"Use musubi_search to find content; the deep-link tool lights up "
            f"when the SDK extension lands."
        )


def _format_row(row: dict[str, Any]) -> str:
    """One-line render for a scrolled episodic row.

    Used by ``fetch_recent_context`` and (via the shared helper in
    ``tools.household``) household status. Kept simple — LLM reads
    this, not a human in a terminal.
    """
    tags = row.get("tags") or []
    agent_tag = next(
        (t for t in tags if isinstance(t, str) and t.endswith("-voice")),
        None,
    )
    speaker = agent_tag.removesuffix("-voice") if agent_tag else (row.get("namespace") or "?")
    content = (row.get("content") or "").strip()
    return f"[{speaker}] {content}"


def _format_search_row(row: dict[str, Any]) -> str:
    """One-line render for a retrieve hit. Surfaces the row's origin
    channel (the ``presence`` segment of the stored namespace) so the
    LLM can attribute "you told me this on Openclaw" vs "on our last
    call". Falls back to the raw namespace if the row's namespace
    isn't 3-segment for any reason."""
    ns = row.get("namespace") or ""
    parts = ns.split("/")
    channel = parts[1] if len(parts) >= 2 else ns or "?"
    content = (row.get("content") or "").strip()
    return f"[{channel}] {content}"


#: One-release deprecation alias. ADR 0032 standardises on
#: ``MusubiToolsMixin``; ``MemoryToolsMixin`` keeps existing imports
#: compiling through the deprecation window. Drops in the next minor
#: release — at which point this line goes away too.
MemoryToolsMixin = MusubiToolsMixin

__all__ = ["MusubiToolsMixin", "MemoryToolsMixin"]
