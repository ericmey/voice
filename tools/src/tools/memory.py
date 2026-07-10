"""MusubiToolsMixin — the agent-tools surface for voice agents.

Three LLM-exposed tools: ``musubi_recent``, ``musubi_search``,
``musubi_remember``. Identical names + parameter shapes across every Musubi
adapter (this mixin, the browser plugin, the Python MCP adapter) so Eric
or any model gets the same surface regardless of modality.

``musubi_think`` (presence-to-presence send) is **retained in code but not
exposed to the LLM** as of 2026-07-10. The live comms webbing reaches peers
via agent-bridge (TUI inject), not a Musubi thought-inbox scroll — no
presence subscribes to ``/v1/thoughts/stream`` for these four agents — so a
phone-side send would land in a plane nobody checks, and a persona that says
"never say you passed something along" cannot truthfully offer it. ``think_impl``
and ``MusubiClient.send_thought`` stay ready; re-add the ``@function_tool``
wrapper if a real consumer is wired. See the deleted ``musubi_think`` tool below.

``musubi_get`` was removed 2026-07-09. It was registered as a tool but
returned a "not yet available" message, because the Python MusubiClient
never gained per-plane ``get(object_id)`` accessors. A prompt-visible tool
the runtime cannot fulfil is a fabrication generator — the same defect that
took ``openclaw_delegate`` down. Reserving a name is not worth teaching the
model to reach for something that isn't there. Use ``musubi_search``.
"""

from __future__ import annotations

import asyncio
import logging
import uuid
from typing import Any

from livekit.agents import Agent, function_tool
from sdk.config import UNCONFIGURED_CONFIG, AgentConfig
from sdk.musubi_client import (
    MusubiAuthError,
    MusubiClient,
    MusubiClientConfig,
    MusubiClientError,
    MusubiError,
    MusubiServerError,
    MusubiTimeoutError,
)
from sdk.trace import trace

logger = logging.getLogger("voice.agent")

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
_RECENT_CONTEXT_TIMEOUT_S = 3.0

# Search-side state filter. Default Musubi retrieve hides `provisional` so
# unscored ambient captures don't pollute results, but a deliberate
# `musubi_remember` from another channel sits as `provisional` until the
# hourly maturation cron runs. For explicit recall we want fresh
# deliberate stores visible immediately — opt into provisional alongside
# the default `(matured, promoted)`. Per Musubi v1.2.0 / state_filter API.
_SEARCH_STATE_FILTER = ["provisional", "matured", "promoted"]


class MusubiToolsMixin(Agent):
    """Provides the canonical agent-tools surface.

    Three LLM-exposed tools: ``musubi_recent``, ``musubi_search``,
    ``musubi_remember``. ``musubi_think`` is retained as ``think_impl`` but
    not registered as a tool (see the module docstring).

    Per-agent scope: each agent reads/writes its own
    ``<agent>/<channel>/episodic`` per ADR 0030. Cross-channel reads
    (``musubi_search``) fan via ``<agent>/*/episodic``.

    Reads:
      - ``self.config.musubi_v2_namespace`` — the ``<agent>/<channel>``
        prefix; the plane segment (``/episodic`` etc.) is appended per
        call. Unset/malformed degrades to "memory unavailable" — there is
        no ``eric/<agent>`` fabrication fallback (see ``_namespace_prefix``).
      - ``MUSUBI_V2_BASE_URL`` / ``MUSUBI_V2_TOKEN`` env.
    """

    config: AgentConfig = UNCONFIGURED_CONFIG

    def _musubi_client(self) -> MusubiClient:
        """One place to construct the client so tests can monkeypatch."""
        return MusubiClient(config=MusubiClientConfig.from_env())

    def _namespace_prefix(self) -> str | None:
        """The validated ``<agent>/<channel>`` prefix, or ``None`` to degrade.

        Single source for every namespace the mixin builds. Returns ``None``
        — degrading the memory op to "unavailable" — when the agent has no
        ``musubi_v2_namespace`` or it isn't the canonical 2-segment
        agent-as-tenant form (ADR 0030). There is deliberately no
        ``eric/<agent>`` fabrication fallback: an unconfigured agent must not
        silently write into a real tenant — that was the misattribution bug.
        """
        prefix = self.config.musubi_v2_namespace
        if not prefix:
            logger.warning(
                "musubi_v2_namespace unset (agent=%r); memory degrades to unavailable",
                self.config.agent_name,
            )
            return None
        if len(prefix.split("/")) != 2:
            logger.warning(
                "musubi_v2_namespace %r is not <agent>/<channel>; memory degrades",
                prefix,
            )
            return None
        return prefix

    def _own_episodic_namespace(self) -> str | None:
        """This agent's own ``<agent>/<channel>/episodic`` namespace, or None."""
        prefix = self._namespace_prefix()
        return f"{prefix}/episodic" if prefix else None

    def _own_thought_namespace(self) -> str | None:
        """This agent's own ``<agent>/<channel>/thought`` namespace, or None."""
        prefix = self._namespace_prefix()
        return f"{prefix}/thought" if prefix else None

    def _own_presence(self) -> str | None:
        """This agent's ``<agent>/<channel>`` presence for ``from_presence``, or None."""
        return self._namespace_prefix()

    def _tenant_wildcard_episodic_namespace(self) -> str | None:
        """Tenant-wide ``<tenant>/*/episodic`` for cross-channel search (ADR 0031).

        Fans an episodic retrieve across every channel the tenant captured
        into; each result row still carries its concrete stored namespace, so
        provenance survives. ``None`` degrades when the prefix is unset or
        malformed.
        """
        prefix = self._namespace_prefix()
        return f"{prefix.split('/')[0]}/*/episodic" if prefix else None

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
        client = self._musubi_client()
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
            rows = await asyncio.wait_for(
                self._scroll_episodic_recent(namespace, limit, required_tag=voice_tag),
                timeout=_RECENT_CONTEXT_TIMEOUT_S,
            )
        except TimeoutError:
            logger.warning(
                "fetch_recent_context: aggregate timeout after %.1fs",
                _RECENT_CONTEXT_TIMEOUT_S,
            )
            return _DEGRADED_LOOKUP
        except (MusubiTimeoutError, MusubiServerError) as err:
            logger.warning("fetch_recent_context: transient %s", err)
            return _DEGRADED_LOOKUP
        except MusubiAuthError as err:
            logger.error("fetch_recent_context: auth failure: %s", err)
            return _DEGRADED_LOOKUP
        except MusubiClientError as err:
            logger.error("fetch_recent_context: bad request: %s", err)
            return _DEGRADED_LOOKUP
        except MusubiError as err:
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

        Unlike musubi_recent (which scrolls your VOICE channel only, by
        recency — not a time window), this searches every surface you
        exist on. If you were told something on another surface, THIS is
        the tool that finds it on a phone call. Each result row carries its origin namespace so you can
        say which surface a memory came from.

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
            response = await self._musubi_client().retrieve(
                namespace=namespace,
                query_text=query,
                mode="deep",
                limit=limit,
                state_filter=_SEARCH_STATE_FILTER,
            )
        except (MusubiTimeoutError, MusubiServerError) as err:
            logger.warning("musubi_search: transient %s", err)
            return _DEGRADED_LOOKUP
        except MusubiAuthError as err:
            logger.error("musubi_search: auth failure: %s", err)
            return _DEGRADED_LOOKUP
        except MusubiClientError as err:
            logger.error("musubi_search: bad request: %s", err)
            return _DEGRADED_LOOKUP
        except MusubiError as err:
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

        Tool name + parameter shape match the browser plugin's
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
            ack = await self._musubi_client().capture_memory(
                namespace=namespace,
                content=content,
                tags=topic_list,
                importance=importance,
                idempotency_key=idem,
            )
        except (MusubiTimeoutError, MusubiServerError) as err:
            logger.warning("musubi_remember: transient %s", err)
            return _DEGRADED_STORE
        except MusubiAuthError as err:
            logger.error("musubi_remember: auth failure: %s", err)
            return "Memory didn't save — auth failed."
        except MusubiClientError as err:
            logger.error("musubi_remember: bad request: %s", err)
            return "Memory didn't save — request rejected."
        except MusubiError as err:
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
        """Presence-to-presence thought send. **Retained but not LLM-exposed**
        (2026-07-10) — the ``@function_tool musubi_think`` wrapper was removed
        because the live webbing does not consume the thought plane for these
        agents (see module docstring). Kept callable for programmatic use and
        so re-enabling the tool is a one-line wrapper away."""
        trace(f"tool=musubi_think to={to_presence!r} content={content[:60]!r} channel={channel!r}")
        prefix = self._namespace_prefix()
        if prefix is None:
            logger.debug("musubi_think: no namespace configured; degrading")
            return _DEGRADED_LOOKUP
        if not to_presence.strip():
            return "Error: to_presence is required."
        if not content.strip():
            return "Error: content is required."

        namespace = f"{prefix}/thought"
        from_presence = prefix
        # Bare ``<agent>`` aliases resolve to ``<agent>/<this-channel>``. The
        # prefix is a validated 2-segment ``<agent>/<channel>`` (agent-as-tenant,
        # ADR 0030), so the channel is always the second segment.
        own_channel = prefix.split("/", 1)[1]
        resolved_to = to_presence if "/" in to_presence else f"{to_presence}/{own_channel}"

        try:
            ack = await self._musubi_client().send_thought(
                namespace=namespace,
                from_presence=from_presence,
                to_presence=resolved_to,
                content=content,
                channel=channel,
                importance=importance,
            )
        except (MusubiTimeoutError, MusubiServerError) as err:
            logger.warning("musubi_think: transient %s", err)
            return "Thought didn't deliver — Musubi is unavailable."
        except MusubiAuthError as err:
            logger.error("musubi_think: auth failure: %s", err)
            return "Thought didn't deliver — auth failed."
        except MusubiClientError as err:
            logger.error("musubi_think: bad request: %s", err)
            return "Thought didn't deliver — request rejected."
        except MusubiError as err:
            logger.warning("musubi_think: %s", err)
            return "Thought didn't deliver — unknown error."

        object_id = ack.get("object_id") or "<unknown>"
        return f"Sent to {resolved_to}. (id={object_id})"

    # NOTE: the ``@function_tool musubi_think`` wrapper was removed 2026-07-10.
    # It told the model "you MUST call this to deliver a note to another agent,"
    # which contradicts every persona's "there is no delegation route from the
    # phone / never say you passed something along" — and the thought plane it
    # wrote to is not consumed by the live comms webbing. ``think_impl`` above
    # stays for programmatic use; re-add a thin ``@function_tool`` wrapper here
    # if a real consumer (SSE subscriber / inbox scroll) is wired for these agents.


def _format_row(row: dict[str, Any]) -> str:
    """One-line render for a scrolled episodic row.

    Used by ``fetch_recent_context``. Kept simple — an LLM reads this,
    not a human in a terminal.
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
    LLM can attribute which surface a memory came from. Falls back to the raw namespace if the row's namespace
    isn't 3-segment for any reason."""
    ns = row.get("namespace") or ""
    parts = ns.split("/")
    channel = parts[1] if len(parts) >= 2 else ns or "?"
    content = (row.get("content") or "").strip()
    return f"[{channel}] {content}"


__all__ = ["MusubiToolsMixin"]
