"""``synthetic-home``: seeded entities and the responder for the home catalog."""

from __future__ import annotations

import random
from typing import Any

try:
    from .home_catalog import INSTRUCTIONS, SPECS
    from .toolkit import CallLog, CollectionResponder, build_server
except ImportError:  # run from a plugin folder
    from home_catalog import INSTRUCTIONS, SPECS  # type: ignore[no-redef]
    from toolkit import CallLog, CollectionResponder, build_server  # type: ignore[no-redef]

SERVER_NAME = "synthetic-home"

_LIGHTS = {
    "kitchen_ceiling": "kitchen",
    "kitchen_island": "kitchen",
    "living_room_lamp": "living_room",
    "living_room_ceiling": "living_room",
    "master_bedroom_ceiling": "master_bedroom",
    "guest_bedroom_lamp": "guest_bedroom",
    "office_desk": "office",
    "hallway": "hallway",
    "garage": "garage",
    "porch": "front_yard",
}
_CLIMATE = ("living_room", "master_bedroom", "guest_bedroom", "office")
_ZONES = ("front_lawn", "back_garden_beds", "vegetable_patch", "hedges")


def _named(domain: str, names: dict[str, str], **attrs: Any) -> dict[str, dict[str, Any]]:
    return {
        f"{domain}.{n}": {"area": area, "name": n.replace("_", " "), **attrs}
        for n, area in names.items()
    }


def home_state(seed: int) -> dict[str, dict[str, dict[str, Any]]]:
    """Every collection the home responder reads, from ``seed``."""
    rng = random.Random(seed)
    lights = _named("light", _LIGHTS, state="off", brightness_pct=0)
    for attrs in lights.values():
        if rng.random() < 0.3:
            attrs.update(state="on", brightness_pct=rng.choice((40, 60, 80, 100)))
    climate = {
        f"climate.{room}": {
            "area": room,
            "hvac_mode": "heat",
            "current_temperature": round(rng.uniform(17.0, 22.5), 1),
            "target_temperature": rng.choice((18.0, 19.0, 20.0, 21.0)),
            "preset": "home",
        }
        for room in _CLIMATE
    }
    sensors = {
        "sensor.living_room_temperature": {"area": "living_room", "unit": "°C"},
        "sensor.guest_bedroom_temperature": {"area": "guest_bedroom", "unit": "°C"},
        "sensor.outdoor_temperature": {"area": "back_garden", "unit": "°C"},
        "sensor.basement_humidity": {"area": "basement", "unit": "%"},
        "sensor.kitchen_co2": {"area": "kitchen", "unit": "ppm"},
        "sensor.power_meter": {"area": "basement", "unit": "W"},
        "sensor.solar_power": {"area": "roof", "unit": "W"},
    }
    for attrs in sensors.values():
        attrs["value"] = round(rng.uniform(5, 900), 1)
    simple = {
        "switch": {"coffee_maker": "kitchen", "garage_heater": "garage", "holiday_lights": "porch"},
        "lock": {"front_door": "hallway", "back_door": "kitchen", "garage_side": "garage"},
        "media_player": {
            "living_room_tv": "living_room",
            "kitchen_speaker": "kitchen",
            "office_speaker": "office",
        },
        "binary_sensor": {
            "front_door_contact": "hallway",
            "basement_water_leak": "basement",
            "hallway_motion": "hallway",
        },
        "cover": {
            "living_room_blinds": "living_room",
            "master_bedroom_blinds": "master_bedroom",
            "garage_door": "garage",
        },
        "fan": {"master_bedroom_ceiling": "master_bedroom", "office_desk": "office"},
        "camera": {
            "front_door": "front_yard",
            "driveway": "front_yard",
            "back_garden": "back_garden",
        },
        "vacuum": {"downstairs": "living_room", "upstairs": "hallway"},
        "irrigation": {zone: "back_garden" if "back" in zone else "front_yard" for zone in _ZONES},
        "automation": dict.fromkeys(
            ("morning_lights", "away_mode", "night_lock", "leak_alert"), ""
        ),
        "scene": dict.fromkeys(("movie_night", "good_morning", "dinner", "away"), ""),
        "water_heater": {"main": "basement"},
        "person": {"alex": "", "sam": ""},
    }
    collections = {
        "light": lights,
        "climate": climate,
        "sensor": sensors,
        **{domain: _named(domain, names, state="idle") for domain, names in simple.items()},
    }
    collections["media"] = collections.pop("media_player")
    return collections


def _extras(seed: int) -> dict[str, Any]:
    rng = random.Random(seed + 1)
    kwh = round(rng.uniform(9, 24), 2)
    return {
        "energy_get_consumption": lambda a: {"period": a.get("period", "day"), "kwh": kwh},
        "energy_get_solar_production": lambda a: {"period": a.get("period"), "kwh": kwh / 2},
        "energy_get_battery_state": lambda a: {"charge_pct": 64, "mode": "self_consumption"},
        "energy_set_battery_mode": lambda a: {"ok": True, "mode": a.get("mode")},
        "energy_get_tariff": lambda a: {"price_per_kwh": 0.27, "next_change": "21:00"},
        "energy_get_device_breakdown": lambda a: {"devices": {"heat_pump": kwh * 0.4}},
        "irrigation_get_schedule": lambda a: {"programme": {"mon": "06:00", "thu": "06:00"}},
        "irrigation_set_rain_delay": lambda a: {"ok": True, "paused_hours": a.get("hours")},
        "alarm_get_state": lambda a: {"state": "disarmed", "open_sensors": []},
        "alarm_arm_home": lambda a: {"ok": True, "state": "armed_home"},
        "alarm_arm_away": lambda a: {"ok": True, "state": "armed_away"},
        "alarm_disarm": lambda a: {"ok": True, "state": "disarmed"},
        "notify_send": lambda a: {"ok": True, "delivered_to": a.get("target")},
        "tts_announce": lambda a: {"ok": True, "speaker": a.get("media_player_id")},
        "area_list": lambda a: {
            "areas": sorted({*_LIGHTS.values(), *_CLIMATE, "back_garden", "basement", "roof"})
        },
        "device_tracker_list": lambda a: {"items": [{"id": "phone_alex", "zone": "home"}]},
    }


def _search(collections: dict[str, dict[str, dict[str, Any]]]) -> Any:
    def search(args: dict[str, Any]) -> dict[str, Any]:
        query = str(args.get("query") or "").lower().replace(" ", "_")
        domain = str(args.get("domain") or "")
        hits = [
            rid
            for rows in collections.values()
            for rid in rows
            if query in rid and (not domain or rid.startswith(f"{domain}."))
        ]
        return {"count": len(hits), "entity_ids": sorted(hits)}

    return search


def build(seed: int, log: CallLog) -> Any:
    """The ``synthetic-home`` FastMCP server."""
    collections = home_state(seed)
    flat = {rid: attrs for rows in collections.values() for rid, attrs in rows.items()}
    extras = _extras(seed)
    extras["entity_search"] = _search(collections)
    extras["entity_get_attributes"] = lambda a: flat.get(
        str(a.get("entity_id")), {"error": "unknown entity"}
    )
    extras["device_list"] = lambda a: {"devices": sorted(flat)[:40]}
    responder = CollectionResponder(collections, extras)
    return build_server(SERVER_NAME, INSTRUCTIONS, SPECS, responder, log)
