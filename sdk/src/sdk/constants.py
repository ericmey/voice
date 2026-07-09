"""Shared constants — Discord channels.

The callback guardrails (delay bounds, quiet hours, the shell sanitizer)
lived here to support ``schedule_callback``, which dialed Eric's phone
through an external CLI gateway. That gateway is retired and the tool is
gone, so the guardrails went with it. If callback scheduling comes back it
should get its own actuator and its own bounds rather than inheriting these.
"""

from __future__ import annotations

# Household Discord targets. Individual agents point at their own room
# via ``AgentConfig.discord_room``; for now every agent uses Nyla's
# channel until per-agent rooms exist. Eric's DM is agent-independent.
NYLA_DISCORD_ROOM = "channel:1480975791977140285"
ERIC_DISCORD_DM = "user:527362260486586368"
