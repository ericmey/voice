"""CoreToolsMixin — get_current_time, get_weather."""

from __future__ import annotations

import logging
import os
from datetime import datetime
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import aiohttp
from livekit.agents import Agent, function_tool
from sdk.trace import trace

logger = logging.getLogger("voice.agent")

# Shared TCP connector for NWS weather calls. Sessions are scoped to
# individual requests, so there is no module-level ClientSession to close.
_weather_connector: aiohttp.TCPConnector | None = None


def _shared_weather_connector() -> aiohttp.TCPConnector:
    global _weather_connector
    if _weather_connector is None or _weather_connector.closed:
        _weather_connector = aiohttp.TCPConnector(limit=5)
    return _weather_connector


# Connector cleanup: each per-request ClientSession owns and closes the
# connector it used. The module-level reference is replaced on the next
# call if that connector is closed.


# ERIC'S TIMEZONE. Not the server's.
#
# `get_current_time` used `datetime.now().astimezone()` and its docstring said, out loud,
# "the current local date and time ON THE SERVER". So on the 2026-07-11 acceptance call she
# told Eric it was "1:48 AM UTC" — four hours wrong, because mizuki's host timezone is
# Etc/UTC. Not the container: THE HOST. There was no correct timezone anywhere on that machine
# to inherit, so no amount of Docker TZ plumbing would have fixed it.
#
# And the fleet already KNEW where he was — `get_weather` has "Carmel, Indiana" hardcoded ten
# lines below. The system knew where ERIC was and asked the machine where IT was.
#
# A person asking an assistant for the time is asking what time it is WHERE THEY ARE. The
# server's location is an accident of provisioning and has never once been the answer. So the
# zone is now an explicit, verifiable piece of config — never ambient machine state.
DEFAULT_TIMEZONE = "America/Indiana/Indianapolis"  # Carmel, IN — matches get_weather
ENV_TIMEZONE = "VOICE_TIMEZONE"


def resolve_timezone() -> ZoneInfo:
    """Eric's zone, from config. Falls back loudly, never silently to the machine's."""
    name = (os.environ.get(ENV_TIMEZONE) or "").strip() or DEFAULT_TIMEZONE
    try:
        return ZoneInfo(name)
    except (ZoneInfoNotFoundError, ValueError, OSError):
        logger.error(
            "%s=%r is not a valid IANA timezone — falling back to %s. NOT falling back to the "
            "server's clock: the server is in Etc/UTC and would be four hours wrong.",
            ENV_TIMEZONE,
            name,
            DEFAULT_TIMEZONE,
        )
        return ZoneInfo(DEFAULT_TIMEZONE)


class CoreToolsMixin(Agent):
    """Provides get_current_time and get_weather tools."""

    @function_tool
    async def get_current_time(self) -> str:
        """Get the current date and time where Eric is (Carmel, Indiana).

        Invocation Condition: Invoke this tool whenever the user asks
        what time it is, what day it is, or the current date. You MUST
        call this tool to get the time. Never guess or estimate the time
        without calling this tool first.
        """
        trace("tool=get_current_time")
        now = datetime.now(resolve_timezone())
        return now.strftime("%A, %B %-d, %Y %-I:%M:%S %p %Z")

    @function_tool
    async def get_weather(self) -> str:
        """Get the current weather conditions in Carmel, Indiana.

        Invocation Condition: Invoke this tool whenever the user asks
        about the weather, temperature, or conditions outside. Examples:
        "What's the weather like?", "Is it cold outside?", "What's the
        temperature?". You MUST call this tool — never guess the weather.
        """
        trace("tool=get_weather")
        nws_url = "https://api.weather.gov/stations/KTYQ/observations/latest"
        headers = {
            "User-Agent": "(livekit-voice-agent, user@example.com)",
            "Accept": "application/geo+json",
        }
        try:
            timeout = aiohttp.ClientTimeout(total=3)
            async with aiohttp.ClientSession(
                connector=_shared_weather_connector(),
                timeout=timeout,
            ) as http:
                async with http.get(nws_url, headers=headers) as resp:
                    if resp.status != 200:
                        trace(f"tool=get_weather NWS status={resp.status}")
                        return f"Couldn't pull weather — NWS returned {resp.status}."
                    data = await resp.json()

            props = data.get("properties", {})
            desc = props.get("textDescription", "")
            temp_c = props.get("temperature", {}).get("value")
            humidity = props.get("relativeHumidity", {}).get("value")
            wind_speed_kmh = props.get("windSpeed", {}).get("value")

            parts: list[str] = []
            if temp_c is not None:
                temp_f = round(temp_c * 9 / 5 + 32)
                parts.append(f"{temp_f} degrees")
            if desc:
                parts.append(desc.lower())
            if humidity is not None:
                parts.append(f"{round(humidity)}% humidity")
            if wind_speed_kmh is not None:
                wind_mph = round(wind_speed_kmh * 0.621371)
                parts.append(f"wind {wind_mph} mph")

            if not parts:
                trace("tool=get_weather NO_DATA")
                return "Couldn't pull weather — NWS returned an empty observation."
            result = ", ".join(parts)
            trace(f"tool=get_weather DONE result={result[:80]}")
            return f"Current conditions in Carmel: {result}."
        except TimeoutError:
            trace("tool=get_weather TIMEOUT")
            return "Couldn't pull weather — NWS didn't respond in time."
        except Exception as err:
            trace(f"tool=get_weather ERROR {err}")
            return "Couldn't pull weather — NWS lookup failed."
