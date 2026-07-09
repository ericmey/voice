"""Discord targets are the only constants left.

The callback guardrails were deleted with ``schedule_callback``; this
test pins that they stay gone, so a future re-add has to bring its own
bounds rather than silently resurrecting the old ones.
"""

import pytest
import sdk.constants as constants
from sdk.constants import ERIC_DISCORD_DM, NYLA_DISCORD_ROOM


def test_discord_targets_are_addressable():
    assert NYLA_DISCORD_ROOM.startswith("channel:")
    assert ERIC_DISCORD_DM.startswith("user:")


@pytest.mark.parametrize(
    "symbol",
    [
        "DELAY_RE",
        "E164_RE",
        "CALLBACK_MIN_DELAY_S",
        "CALLBACK_MAX_DELAY_S",
        "CALLBACK_SHORT_DELAY_S",
        "CALLBACK_QUIET_START_HOUR",
        "CALLBACK_QUIET_END_HOUR",
        "ERIC_TZ",
        "sanitize",
        "parse_delay_seconds",
        "is_quiet_hour",
    ],
)
def test_callback_guardrails_are_gone(symbol):
    assert not hasattr(constants, symbol), f"{symbol} outlived schedule_callback"
