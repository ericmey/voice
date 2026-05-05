"""Shared constants — Discord channels, sanitizer regexes, callback guardrails."""

from __future__ import annotations

import re
import shlex
from zoneinfo import ZoneInfo

# Household Discord targets. Individual agents point at their own room
# via ``AgentConfig.discord_room``; for now every agent uses Nyla's
# channel until per-agent rooms exist. Eric's DM is agent-independent.
NYLA_DISCORD_ROOM = "channel:1480975791977140285"
ERIC_DISCORD_DM = "user:527362260486586368"

# schedule_callback sanitizer — use shlex.quote for robust shell-escaping
# rather than an incomplete regex that can miss unicode homoglyphs and
# control characters.
DELAY_RE = re.compile(r"^\d+[mhd]$")
E164_RE = re.compile(r"^\+[1-9]\d{1,14}$")

# --- schedule_callback guardrails --------------------------------------
# Callback scheduling is a real actuator — it dials Eric's phone at a
# future moment. These thresholds turn "please remember to be careful"
# into deterministic bounds. Override via AgentConfig later if agents
# need different limits.
CALLBACK_MIN_DELAY_S = 60  # anything under 1 min is rejected outright
CALLBACK_MAX_DELAY_S = 24 * 3600  # anything over 24h should be a cron, not a callback
CALLBACK_SHORT_DELAY_S = 120  # under 2 min → require explicit confirmed=True

# Eric's local timezone — used to decide quiet hours when the callback
# would fire. Callbacks during 22:00-07:00 local require confirmed=True.
ERIC_TZ = ZoneInfo("America/Indiana/Indianapolis")
CALLBACK_QUIET_START_HOUR = 22  # inclusive
CALLBACK_QUIET_END_HOUR = 7  # exclusive


def sanitize(text: str) -> str:
    """Shell-safe a caller-supplied string using stdlib shlex.quote.

    Wraps the text so a shell will treat it as a single literal argument,
    preventing injection of spaces, quotes, backslashes, and other
    metacharacters. This is more reliable than a blocklist regex because
    it does not need to enumerate every possible shell-meaningful character.
    """
    return shlex.quote(text or "")


def parse_delay_seconds(delay: str) -> int:
    """Convert ``"5m" / "1h" / "2d"`` into seconds. Assumes caller has
    already validated against ``DELAY_RE``; returns ``-1`` on mismatch
    so the caller can surface an error without this helper raising.
    """
    if not DELAY_RE.match(delay):
        return -1
    unit = delay[-1]
    num = int(delay[:-1])
    multiplier = {"m": 60, "h": 3600, "d": 86400}[unit]
    return num * multiplier


def is_quiet_hour(hour: int) -> bool:
    """Is the given local-clock hour inside the configured quiet window?

    Quiet window is ``CALLBACK_QUIET_START_HOUR`` inclusive through
    ``CALLBACK_QUIET_END_HOUR`` exclusive, wrapping across midnight.
    """
    return hour >= CALLBACK_QUIET_START_HOUR or hour < CALLBACK_QUIET_END_HOUR
