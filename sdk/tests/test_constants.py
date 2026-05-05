"""Tests for constants module."""

from sdk.constants import (
    DELAY_RE,
    E164_RE,
    ERIC_DISCORD_DM,
    NYLA_DISCORD_ROOM,
    sanitize,
)


def test_discord_channels_are_numeric_strings():
    assert NYLA_DISCORD_ROOM.startswith("channel:")
    assert ERIC_DISCORD_DM.startswith("user:")


def test_sanitize_uses_shlex_quote():
    # shlex.quote wraps strings containing shell metacharacters
    assert sanitize('hello "world"') == "'hello \"world\"'"
    # Simple strings without shell-meaningful characters pass through unchanged
    assert sanitize("safetext") == "safetext"
    # Malicious input is safely quoted, not stripped
    assert sanitize("rm -rf /; echo bad") == "'rm -rf /; echo bad'"


def test_delay_regex_accepts_valid():
    assert DELAY_RE.match("5m")
    assert DELAY_RE.match("30m")
    assert DELAY_RE.match("1h")
    assert DELAY_RE.match("2d")


def test_delay_regex_rejects_invalid():
    assert not DELAY_RE.match("5")
    assert not DELAY_RE.match("m")
    assert not DELAY_RE.match("5x")
    assert not DELAY_RE.match("")


def test_e164_regex_accepts_valid():
    assert E164_RE.match("+13175551234")
    assert E164_RE.match("+442071234567")


def test_e164_regex_rejects_invalid():
    assert not E164_RE.match("3175551234")
    assert not E164_RE.match("+0123")
    assert not E164_RE.match("")
