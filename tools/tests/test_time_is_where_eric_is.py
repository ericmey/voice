"""The time she tells him must be HIS time, not the server's.

On the 2026-07-11 acceptance call Aoi told Eric it was "1:48 AM UTC". It was 9:48 PM on
Saturday, where he was. Not merely four hours wrong — THE WRONG DAY.

The tool's own docstring said it out loud: "the current local date and time ON THE SERVER."
It did exactly what it claimed. `datetime.now().astimezone()` faithfully rendered the machine's
zone, and mizuki's HOST is Etc/UTC — not the container, the host. So there was no correct
timezone anywhere on that box to inherit, and no amount of Docker TZ plumbing would have fixed
it.

Meanwhile `get_weather` has "Carmel, Indiana" hardcoded. The system knew where ERIC was and
asked the machine where IT was.

A person asking for the time is asking what time it is WHERE THEY ARE. The server's location
is an accident of provisioning and has never once been the answer.
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest
from tools.core import DEFAULT_TIMEZONE, ENV_TIMEZONE, resolve_timezone


def test_the_default_zone_is_where_eric_lives() -> None:
    """Carmel, Indiana — the same place `get_weather` is hardcoded to."""
    assert DEFAULT_TIMEZONE == "America/Indiana/Indianapolis"
    assert str(resolve_timezone()) == DEFAULT_TIMEZONE


def test_the_time_is_not_utc(monkeypatch: pytest.MonkeyPatch) -> None:
    """THE BUG. She said 'Sunday, July 12th, 1:48 AM UTC' on a Saturday evening."""
    monkeypatch.delenv(ENV_TIMEZONE, raising=False)

    here = datetime.now(resolve_timezone())
    utc = datetime.now(UTC)

    assert here.utcoffset() != utc.utcoffset(), (
        "the time tool is rendering UTC — that is the server's location, not Eric's, and on "
        "the acceptance call it gave him the wrong DAY"
    )


def test_the_zone_is_configurable(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(ENV_TIMEZONE, "Europe/Berlin")
    assert str(resolve_timezone()) == "Europe/Berlin"


def test_a_bad_zone_falls_back_to_eric_not_to_the_server(monkeypatch: pytest.MonkeyPatch) -> None:
    """A typo must not silently drop us back onto the machine's clock — which is Etc/UTC and
    would be four hours wrong, which is exactly the failure we are fixing."""
    monkeypatch.setenv(ENV_TIMEZONE, "Mars/Olympus_Mons")

    assert str(resolve_timezone()) == DEFAULT_TIMEZONE
