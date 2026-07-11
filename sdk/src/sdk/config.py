"""AgentConfig — the single source of truth for a voice agent's identity.

Every identity axis derives from one root, ``agent_name``:

- ``agent_name`` (``"aoi"``)            → memory namespace + tag, and the
  ``$AGENT`` / ``VOICE_AGENT_NAME`` env the entrypoint sets.
- ``registration_name`` (``"phone-aoi"``) → the LiveKit worker registration
  name, the telemetry ``agent`` field, transcript/recording tags. Derived,
  never hand-typed, so the three spellings can't drift apart.

Before this was the single source, "who am I" was asserted in three
disconnected places — the config, hand-typed ``"phone-nyla"`` literals in
each entrypoint, and ``$AGENT`` — that never cross-checked. Centralizing it
here is what keeps a specialist's memories from being attributed to Nyla and
her telemetry from landing under another service name.
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass

logger = logging.getLogger("voice.agent")


@dataclass(frozen=True)
class AgentConfig:
    """Per-agent identity. One root (``agent_name``); everything derives.

    Attributes:
        agent_name: Short canonical id ("nyla", "aoi", "yua", "sumi"). The
            root of every other identity axis. Also the ``$AGENT`` value the
            entrypoint resolves the per-agent secrets from, and the
            ``VOICE_AGENT_NAME`` that becomes the OTel ``service.name``
            (``voice-<agent_name>``).
        memory_agent_tag: Value written into stored Musubi memories'
            ``payload.agent`` field, and the tag the greeting hook filters
            on. Separates voice identities so callers can filter memories by
            speaker. Distinct per agent (e.g. ``aoi-voice``) — sharing it is
            how one agent's calls bleed into another's memory.
        musubi_v2_namespace: Two-segment ``<agent>/<channel>`` prefix
            (Musubi ADR 0030 agent-as-tenant form) used by the Musubi tools
            in ``tools/memory.py``. The mixin appends the plane segment at
            call time: ``<prefix>/episodic`` for remember/recent,
            ``<prefix>/thought`` for think; ``musubi_search`` widens to
            ``<tenant>/*/episodic`` per ADR 0031. Example: ``aoi/voice``.
            ``None`` means memory is not configured for this agent — the
            tools degrade to "memory unavailable" rather than writing into
            a fallback tenant. There is deliberately no ``eric/<agent>``
            fabrication fallback: an unconfigured namespace fails loud
            (degrades), it does not silently pick a real tenant.
    """

    agent_name: str
    memory_agent_tag: str
    musubi_v2_namespace: str | None = None

    @property
    def registration_name(self) -> str:
        """The ``phone-<agent_name>`` string LiveKit registers under and
        telemetry/transcripts tag with. Derived so it can never drift from
        ``agent_name``."""
        return f"phone-{self.agent_name}"

    def __post_init__(self) -> None:
        """THE MEMORY FENCE. The namespace tenant must BE the agent.

        ``assert_agent_identity`` proves ``$AGENT == agent_name``. It says nothing about
        ``musubi_v2_namespace`` — and that is the field which decides WHERE THE MEMORIES GO.
        So this passed, at the reviewed head::

            AgentConfig(agent_name="aoi", musubi_v2_namespace="nyla/voice")

        Aoi answers the phone as Aoi. She registers as ``phone-aoi``. Her identity assert
        passes. And every memory she writes lands in ``nyla/voice/episodic``, while
        ``musubi_search`` widens to ``nyla/*/episodic`` and reads Nyla's whole tenant back to
        her.

        That is the exact misattribution this module exists to prevent, sitting inside the
        fence built to prevent it. Three heads of the identity model were closed and the one
        that actually routes the memories was left open. (Yua, QA of 96e2388.)

        Validated at CONSTRUCTION, not at startup, and that distinction is the point:
        ``assert_agent_identity`` is skipped when ``VOICE_AGENT_NAME`` is unset (dev, tests,
        any direct import). A config that cannot be BUILT wrong cannot be shipped wrong from
        any entry point at all.
        """
        if not self.agent_name or not self.agent_name.strip():
            raise ValueError("AgentConfig.agent_name is required — an agent has a name")

        ns = self.musubi_v2_namespace
        if ns is None:
            return  # memory deliberately unconfigured; tools degrade to "unavailable"

        parts = ns.split("/")
        if len(parts) != 2 or not all(p.strip() for p in parts):
            raise ValueError(
                f"AgentConfig.musubi_v2_namespace={ns!r} is not a canonical "
                f"'<agent>/<channel>' pair. Empty or extra segments would silently reshape "
                f"the Musubi path the memory tools build."
            )

        tenant = parts[0]
        if tenant != self.agent_name:
            raise ValueError(
                f"MEMORY FENCE VIOLATION: agent_name={self.agent_name!r} but "
                f"musubi_v2_namespace={ns!r} — the tenant is {tenant!r}.\n"
                f"{self.agent_name} would answer the phone as herself, register as "
                f"phone-{self.agent_name}, pass the startup identity assert, and write every "
                f"memory into {tenant}'s namespace. musubi_search would widen to "
                f"{tenant}/*/episodic and read {tenant}'s whole tenant back to her.\n"
                f"The namespace tenant MUST be the agent. It is not a separate knob."
            )


# Fail-loud sentinel. This is the class-level default on the tool mixins, so
# a new agent that forgets to set ``config`` gets THIS, not a real identity.
# Its namespace is ``None`` — every memory op degrades to "unavailable" and
# ``assert_agent_identity`` raises at startup (``__unconfigured__`` can never
# equal a real ``$AGENT``). That is strictly safer than the old
# ``NYLA_DEFAULT_CONFIG``, which silently attributed an unconfigured agent's
# memories to Nyla — the exact bug this module exists to prevent.
UNCONFIGURED_CONFIG = AgentConfig(
    agent_name="__unconfigured__",
    memory_agent_tag="",
    musubi_v2_namespace=None,
)


def assert_agent_identity(config: AgentConfig) -> None:
    """Fail loud at startup if the env ``$AGENT`` / ``VOICE_AGENT_NAME`` does
    not match ``config.agent_name``.

    This is the cross-check that would have caught an agent registering as
    ``phone-<x>`` while its config claimed to be someone else. Skipped when
    ``VOICE_AGENT_NAME`` is unset (dev/test runs outside the entrypoint).
    """
    env_name = (os.environ.get("VOICE_AGENT_NAME") or "").strip().lower()
    if not env_name:
        return
    if env_name != config.agent_name:
        raise RuntimeError(
            f"identity mismatch: VOICE_AGENT_NAME={env_name!r} but "
            f"config.agent_name={config.agent_name!r}. The entrypoint's $AGENT "
            f"and the agent's AgentConfig must agree — one of them is wrong."
        )
