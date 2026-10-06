"""Read a live forecast for the agent; the A2UI weather renderer never fetches it."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import httpx

from clio_agent.gact.agents.tool_instrumentation import native_tool

_GEOCODE_URL = "https://geocoding-api.open-meteo.com/v1/search"
_FORECAST_URL = "https://api.open-meteo.com/v1/forecast"


def _condition(code: int) -> str:
    """Give a short human condition for an Open-Meteo WMO weather code."""
    if code == 0:
        return "Clear"
    if code in (1, 2):
        return "Partly cloudy"
    if code == 3:
        return "Overcast"
    if code in (45, 48):
        return "Fog"
    if code in (51, 53, 55, 56, 57):
        return "Drizzle"
    if code in (61, 63, 65, 66, 67, 80, 81, 82):
        return "Rain"
    if code in (71, 73, 75, 77, 85, 86):
        return "Snow"
    if code in (95, 96, 99):
        return "Thunderstorms"
    return "Variable conditions"


def _failure(code: str, message: str) -> dict[str, Any]:
    """Return an actionable tool failure without an invented forecast."""
    return {"ok": False, "error": {"code": code, "message": message}}


def _local_timestamp(value: str, zone: ZoneInfo) -> str:
    """Attach the location's actual offset, including a forecast DST change."""
    return datetime.fromisoformat(value).replace(tzinfo=zone).isoformat(timespec="minutes")


def fetch_weather_forecast(
    location: str, days_ahead: int = 6, temperature_unit: str = "F"
) -> dict[str, Any]:
    """Fetch current, hourly, and daily conditions for one named location.

    The result's ``weather`` object uses the clio.weather.v1 field names and
    explicit-offset hourly times. The agent can present it without guessing
    values. ``days_ahead`` is an offset from the location's current date, and
    the response includes that current date. This tool reads Open-Meteo only;
    it does not create a surface.
    """
    place = location.strip()
    if len(place) < 2 or len(place) > 120:
        return _failure("invalid_location", "Give a city or site name of 2 to 120 characters.")
    if not 0 <= days_ahead <= 7:
        return _failure("invalid_days_ahead", "Choose 0 to 7 days ahead of today.")
    if temperature_unit not in ("C", "F"):
        return _failure("invalid_unit", "temperature_unit must be C or F.")

    try:
        with httpx.Client(timeout=12.0, follow_redirects=False) as client:
            geocode = client.get(_GEOCODE_URL, params={"name": place, "count": 1, "language": "en"})
            geocode.raise_for_status()
            matches = geocode.json().get("results") or []
            if not matches:
                return _failure(
                    "location_not_found", f"No location matched {place!r}; add a region."
                )
            match = matches[0]
            forecast = client.get(
                _FORECAST_URL,
                params={
                    "latitude": match["latitude"],
                    "longitude": match["longitude"],
                    "current": "temperature_2m,weather_code,wind_speed_10m",
                    "hourly": "temperature_2m,precipitation_probability,weather_code",
                    "daily": (
                        "temperature_2m_max,temperature_2m_min,"
                        "precipitation_probability_max,weather_code"
                    ),
                    "timezone": "auto",
                    "temperature_unit": "fahrenheit" if temperature_unit == "F" else "celsius",
                    "wind_speed_unit": "mph" if temperature_unit == "F" else "kmh",
                    "forecast_days": days_ahead + 1,
                },
            )
            forecast.raise_for_status()
            data = forecast.json()
    except (httpx.HTTPError, ValueError, KeyError, TypeError, AttributeError) as exc:
        return _failure("forecast_unavailable", f"Could not read the live forecast: {exc}")

    try:
        zone_name = str(data["timezone"])
        zone = ZoneInfo(zone_name)
        current = data["current"]
        hourly = data["hourly"]
        daily = data["daily"]
        current_time = datetime.fromisoformat(_local_timestamp(current["time"], zone))
        hours: list[dict[str, Any]] = []
        for index, local_time in enumerate(hourly["time"]):
            stamp = _local_timestamp(local_time, zone)
            if datetime.fromisoformat(stamp) < current_time:
                continue
            temperature = hourly["temperature_2m"][index]
            if temperature is None:
                continue
            chance = hourly["precipitation_probability"][index]
            hours.append(
                {
                    "time": stamp,
                    "condition": _condition(int(hourly["weather_code"][index])),
                    "temperature": temperature,
                    **({"precipitationChance": chance} if chance is not None else {}),
                }
            )
        days_out = [
            {
                "date": date,
                "condition": _condition(int(daily["weather_code"][index])),
                "high": daily["temperature_2m_max"][index],
                "low": daily["temperature_2m_min"][index],
                **(
                    {"precipitationChance": daily["precipitation_probability_max"][index]}
                    if daily["precipitation_probability_max"][index] is not None
                    else {}
                ),
            }
            for index, date in enumerate(daily["time"])
        ]
        label = ", ".join(
            str(part)
            for part in (match.get("name"), match.get("admin1"), match.get("country"))
            if part
        )
        weather = {
            "location": label,
            "timeZone": zone_name,
            "observedAt": _local_timestamp(current["time"], zone),
            "condition": _condition(int(current["weather_code"])),
            "temperature": current["temperature_2m"],
            "temperatureUnit": temperature_unit,
            "windSpeed": current["wind_speed_10m"],
            "windUnit": "mph" if temperature_unit == "F" else "km/h",
            "source": "Open-Meteo forecast",
            "hourly": hours[:168],
            "daily": days_out,
        }
        from clio_schemas.a2ui.v0_9_1.components import WeatherComponent  # noqa: PLC0415

        WeatherComponent.model_validate(
            {"id": "weather", "component": "clio.weather.v1", **weather}
        )
    except (KeyError, TypeError, ValueError, AttributeError, ZoneInfoNotFoundError) as exc:
        return _failure("forecast_shape", f"Forecast data could not form a weather view: {exc}")

    return {
        "ok": True,
        "weather": weather,
        "latitude": match["latitude"],
        "longitude": match["longitude"],
        "fetchedAt": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "sourceUrl": "https://open-meteo.com/en/docs",
    }


def build_weather_forecast_tool() -> Any:
    """Expose a read-only forecast lookup to a root agent with A2UI."""

    return native_tool(
        fetch_weather_forecast,
        name="get_weather_forecast",
        presentation="text",
        title="Get weather forecast",
        domain="resources",
        desc=(
            "Get live current, hourly, and daily weather for a named location. Returns a "
            "validated clio.weather.v1 data object with local times and explicit UTC offsets; "
            "present it with create_a2ui_surface when a forecast view helps. The tool returns "
            "seven calendar dates beginning today at the location; show the hours and days "
            "relevant to the user's question."
        ),
        args={
            "location": {
                "type": "string",
                "description": "City or site, with region if ambiguous.",
            },
            "temperature_unit": {"type": "string", "description": "C or F; default F."},
        },
        read_only=True,
    )


__all__ = ["build_weather_forecast_tool", "fetch_weather_forecast"]
