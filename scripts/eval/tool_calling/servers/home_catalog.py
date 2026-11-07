"""The ``synthetic-home`` tool catalog: about 90 smart-home tools."""

from __future__ import annotations

try:
    from .toolkit import Param, ToolSpec, p, t
except ImportError:  # run from a plugin folder
    from toolkit import Param, ToolSpec, p, t  # type: ignore[no-redef]

_E = "Entity id, for example {example}."


def _eid(example: str) -> Param:
    return p("entity_id", "str", _E.format(example=example))


_AREA = p("area", "str?", "Only entities in this area, for example kitchen.")
_CODE = p("code", "str", "The four to eight digit alarm or lock code.")
_HVAC = "enum:heat|cool|auto|off|dry|fan_only"

LIGHTS = [
    t("light_list", "list", "List lights with their on/off state and brightness.", _AREA),
    t("light_get_state", "read", "Read one light's state.", _eid("light.kitchen_ceiling")),
    t(
        "light_turn_on",
        "write",
        "Turn a light on, optionally at a brightness and colour temperature.",
        _eid("light.kitchen_ceiling"),
        p("brightness_pct", "int?", "Brightness from 1 to 100."),
        p("color_temp_kelvin", "int?", "White colour temperature, 2000 to 6500 K."),
        p("transition_s", "float", "Fade time in seconds.", 0.0),
    ),
    t("light_turn_off", "write", "Turn a light off.", _eid("light.porch")),
    t("light_toggle", "write", "Toggle a light.", _eid("light.hallway")),
    t(
        "light_set_color",
        "write",
        "Set a colour light to an RGB colour.",
        _eid("light.living_room_lamp"),
        p("rgb_hex", "str", "Colour as #RRGGBB."),
    ),
    t(
        "light_set_brightness",
        "write",
        "Change brightness without changing on/off state.",
        _eid("light.office_desk"),
        p("brightness_pct", "int", "Brightness from 1 to 100."),
    ),
]

SWITCHES = [
    t("switch_list", "list", "List smart plugs and switches.", _AREA),
    t("switch_get_state", "read", "Read one switch.", _eid("switch.coffee_maker")),
    t("switch_turn_on", "write", "Turn a switch on.", _eid("switch.coffee_maker")),
    t("switch_turn_off", "write", "Turn a switch off.", _eid("switch.garage_heater")),
]

CLIMATE = [
    t("climate_list", "list", "List thermostats with current and target temperature.", _AREA),
    t("climate_get_state", "read", "Read one thermostat.", _eid("climate.living_room")),
    t(
        "climate_set_temperature",
        "write",
        "Set a thermostat's target temperature in degrees Celsius.",
        _eid("climate.living_room"),
        p(
            "temperature",
            "float",
            "Target temperature in degrees Celsius.",
            attribute="target_temperature",
        ),
        p("hvac_mode", f"{_HVAC}?", "Also switch to this mode."),
    ),
    t(
        "climate_set_hvac_mode",
        "write",
        "Switch a thermostat between heating, cooling and off.",
        _eid("climate.office"),
        p("hvac_mode", _HVAC, "The new mode."),
    ),
    t(
        "climate_set_fan_mode",
        "write",
        "Set the air handler fan mode.",
        _eid("climate.office"),
        p("fan_mode", "enum:auto|low|medium|high", "The fan mode."),
    ),
    t(
        "climate_set_preset",
        "write",
        "Apply a comfort preset.",
        _eid("climate.master_bedroom"),
        p("preset", "enum:home|away|eco|sleep|boost", "The preset."),
    ),
    t("climate_get_schedule", "read", "Read the weekly heating schedule.", _eid("climate.office")),
    t(
        "climate_set_schedule",
        "write",
        "Replace one day of the weekly heating schedule.",
        _eid("climate.office"),
        p("day", "enum:mon|tue|wed|thu|fri|sat|sun", "Day of the week."),
        p("periods", "str", "Comma separated HH:MM=°C periods, e.g. 06:30=20,22:00=17."),
    ),
]

LOCKS = [
    t("lock_list", "list", "List door locks and whether each is locked.", _AREA),
    t("lock_get_state", "read", "Read one lock.", _eid("lock.front_door")),
    t("lock_lock", "write", "Lock a door.", _eid("lock.back_door")),
    t("lock_unlock", "write", "Unlock a door.", _eid("lock.front_door"), _CODE),
    t(
        "lock_get_access_log",
        "read",
        "Recent lock and unlock events for a door.",
        _eid("lock.front_door"),
        p("limit", "int", "How many events.", 20),
    ),
]

MEDIA = [
    t("media_list_players", "list", "List TVs and speakers.", _AREA),
    t("media_get_state", "read", "What a player is playing.", _eid("media_player.living_room_tv")),
    t("media_play", "write", "Resume playback.", _eid("media_player.kitchen_speaker")),
    t("media_pause", "write", "Pause playback.", _eid("media_player.kitchen_speaker")),
    t("media_stop", "write", "Stop playback.", _eid("media_player.living_room_tv")),
    t("media_next_track", "write", "Skip to the next track.", _eid("media_player.office_speaker")),
    t(
        "media_set_volume",
        "write",
        "Set a player's volume.",
        _eid("media_player.kitchen_speaker"),
        p("volume_pct", "int", "Volume from 0 to 100."),
    ),
    t(
        "media_play_uri",
        "write",
        "Play a stream, playlist or file URI.",
        _eid("media_player.office_speaker"),
        p("uri", "str", "The media URI."),
        p("shuffle", "bool", "Shuffle a playlist.", False),
    ),
    t(
        "media_select_source",
        "write",
        "Switch a TV or receiver input.",
        _eid("media_player.living_room_tv"),
        p("source", "str", "Input name, e.g. HDMI 2."),
    ),
]

SENSORS = [
    t(
        "sensor_list",
        "list",
        "List numeric sensors (temperature, humidity, power, air quality).",
        _AREA,
        p("device_class", "str?", "Only this class, e.g. temperature."),
    ),
    t("sensor_get_state", "read", "Read a sensor's value.", _eid("sensor.outdoor_temperature")),
    t(
        "sensor_get_history",
        "read",
        "Hourly values of a sensor.",
        _eid("sensor.basement_humidity"),
        p("hours", "int", "How many hours back.", 24),
    ),
    t(
        "sensor_get_statistics",
        "read",
        "Mean, min and max of a sensor over a period.",
        _eid("sensor.power_meter"),
        p("period", "enum:hour|day|week|month", "The period.", "day"),
    ),
    t("binary_sensor_list", "list", "List contact, motion and leak sensors.", _AREA),
    t(
        "binary_sensor_get_state",
        "read",
        "Read an on/off sensor.",
        _eid("binary_sensor.front_door_contact"),
    ),
]

COVERS_FANS = [
    t("cover_list", "list", "List blinds, shades and the garage door.", _AREA),
    t("cover_open", "write", "Open a cover fully.", _eid("cover.living_room_blinds")),
    t("cover_close", "write", "Close a cover fully.", _eid("cover.garage_door")),
    t(
        "cover_set_position",
        "write",
        "Move a cover to a position.",
        _eid("cover.master_bedroom_blinds"),
        p("position_pct", "int", "0 is closed, 100 is open."),
    ),
    t("cover_stop", "write", "Stop a moving cover.", _eid("cover.garage_door")),
    t("fan_list", "list", "List fans.", _AREA),
    t(
        "fan_turn_on",
        "write",
        "Turn a fan on at a speed.",
        _eid("fan.office_desk"),
        p("speed_pct", "int", "Speed from 1 to 100.", 50),
    ),
    t("fan_turn_off", "write", "Turn a fan off.", _eid("fan.master_bedroom_ceiling")),
]

AUTOMATIONS = [
    t("automation_list", "list", "List automations and whether each is enabled."),
    t(
        "automation_get_config",
        "read",
        "Read an automation's triggers, conditions and actions.",
        p("automation_id", "str", "Automation id, e.g. automation.night_lock."),
    ),
    t(
        "automation_enable",
        "write",
        "Enable an automation.",
        p("automation_id", "str", "Automation id."),
    ),
    t(
        "automation_disable",
        "write",
        "Disable an automation.",
        p("automation_id", "str", "Automation id."),
    ),
    t(
        "automation_trigger",
        "write",
        "Run an automation's actions now.",
        p("automation_id", "str", "Automation id."),
        p("skip_conditions", "bool", "Ignore its conditions.", False),
    ),
    t(
        "automation_get_trace",
        "read",
        "The last runs of an automation and why each stopped.",
        p("automation_id", "str", "Automation id."),
        p("limit", "int", "How many runs.", 5),
    ),
    t("scene_list", "list", "List saved scenes."),
    t(
        "scene_activate",
        "write",
        "Apply a scene.",
        p("scene_id", "str", "Scene id, e.g. scene.movie_night."),
    ),
    t(
        "scene_create",
        "write",
        "Save the current state of some entities as a new scene.",
        p("name", "str", "Scene name."),
        p("entity_ids", "str", "Comma separated entity ids."),
    ),
    t("scene_delete", "write", "Delete a scene.", p("scene_id", "str", "Scene id.")),
]

ENERGY = [
    t(
        "energy_get_consumption",
        "read",
        "Household electricity use over a period, in kWh.",
        p("period", "enum:day|week|month", "The period.", "day"),
        p("start_date", "str?", "First day, YYYY-MM-DD; default the current period."),
    ),
    t(
        "energy_get_solar_production",
        "read",
        "Solar production over a period.",
        p("period", "enum:day|week|month", "The period.", "day"),
    ),
    t("energy_get_battery_state", "read", "Home battery charge, power and mode."),
    t(
        "energy_set_battery_mode",
        "write",
        "Change how the home battery is used.",
        p("mode", "enum:self_consumption|backup|time_of_use", "The mode."),
    ),
    t("energy_get_tariff", "read", "Current electricity price and the next price change."),
    t(
        "energy_get_device_breakdown",
        "read",
        "Electricity use per metered device.",
        p("period", "enum:day|week|month", "The period.", "day"),
    ),
]

CAMERAS = [
    t("camera_list", "list", "List cameras.", _AREA),
    t("camera_get_snapshot", "read", "A still image URL.", _eid("camera.front_door")),
    t(
        "camera_get_motion_events",
        "read",
        "Motion events recorded by a camera.",
        _eid("camera.driveway"),
        p("hours", "int", "How many hours back.", 12),
    ),
    t("camera_enable_motion_detection", "write", "Arm motion.", _eid("camera.back_garden")),
    t("camera_disable_motion_detection", "write", "Disarm motion.", _eid("camera.back_garden")),
]

VACUUM = [
    t("vacuum_list", "list", "List robot vacuums."),
    t("vacuum_get_state", "read", "Battery, status and bin.", _eid("vacuum.downstairs")),
    t(
        "vacuum_start_cleaning",
        "write",
        "Start a cleaning run, optionally only some rooms.",
        _eid("vacuum.downstairs"),
        p("rooms", "str?", "Comma separated room names."),
    ),
    t("vacuum_pause", "write", "Pause cleaning.", _eid("vacuum.upstairs")),
    t("vacuum_return_to_dock", "write", "Send it home.", _eid("vacuum.upstairs")),
    t(
        "vacuum_set_suction",
        "write",
        "Set suction power.",
        _eid("vacuum.downstairs"),
        p("level", "enum:quiet|standard|turbo|max", "Suction level."),
    ),
]

IRRIGATION = [
    t("irrigation_list_zones", "list", "List sprinkler and drip zones."),
    t(
        "irrigation_run_zone",
        "write",
        "Open one irrigation zone's valve for a number of minutes.",
        p("zone_id", "str", "Zone id, e.g. irrigation.front_lawn."),
        p("duration_minutes", "int", "Minutes to run, 1 to 120."),
    ),
    t("irrigation_stop_zone", "write", "Close a zone's valve.", p("zone_id", "str", "Zone id.")),
    t("irrigation_get_schedule", "read", "The weekly irrigation programme."),
    t(
        "irrigation_set_rain_delay",
        "write",
        "Pause all scheduled irrigation.",
        p("hours", "int", "Hours to pause."),
    ),
]

SECURITY = [
    t("alarm_get_state", "read", "Whether the alarm is armed and any open sensors."),
    t("alarm_arm_home", "write", "Arm perimeter sensors only.", _CODE),
    t("alarm_arm_away", "write", "Arm every sensor.", _CODE),
    t("alarm_disarm", "write", "Disarm the alarm.", _CODE),
    t("water_heater_get_state", "read", "Tank temperature and mode.", _eid("water_heater.main")),
    t(
        "water_heater_set_temperature",
        "write",
        "Set the tank temperature.",
        _eid("water_heater.main"),
        p("temperature", "float", "Degrees Celsius."),
    ),
    t(
        "water_heater_set_mode",
        "write",
        "Set the water heater mode.",
        _eid("water_heater.main"),
        p("mode", "enum:eco|performance|vacation|off", "The mode."),
    ),
]

PEOPLE = [
    t("person_list", "list", "List household members and whether each is home."),
    t(
        "person_get_location",
        "read",
        "Where a person is.",
        p("person_id", "str", "Person id, e.g. person.alex."),
    ),
    t("device_tracker_list", "list", "List tracked phones and their last zone."),
    t(
        "notify_send",
        "write",
        "Send a push notification.",
        p("target", "str", "A person id or 'all'."),
        p("message", "str", "Message text."),
        p("title", "str?", "Optional title."),
    ),
    t(
        "tts_announce",
        "write",
        "Speak a message on a speaker.",
        p("media_player_id", "str", "Speaker entity id."),
        p("message", "str", "What to say."),
    ),
]

REGISTRY = [
    t("area_list", "list", "List the home's areas (rooms and outdoor spaces)."),
    t(
        "entity_search",
        "read",
        "Find entities whose id or name contains some text.",
        p("query", "str", "Text to look for."),
        p("domain", "str?", "Only this domain, e.g. light."),
    ),
    t("device_list", "list", "List physical devices.", _AREA),
    t(
        "entity_get_attributes",
        "read",
        "Every attribute of any entity.",
        _eid("sensor.kitchen_co2"),
    ),
]

SPECS: list[ToolSpec] = [
    *LIGHTS,
    *SWITCHES,
    *CLIMATE,
    *LOCKS,
    *MEDIA,
    *SENSORS,
    *COVERS_FANS,
    *AUTOMATIONS,
    *ENERGY,
    *CAMERAS,
    *VACUUM,
    *IRRIGATION,
    *SECURITY,
    *PEOPLE,
    *REGISTRY,
]

INSTRUCTIONS = (
    "A smart home: lights, switches, thermostats, locks, media players, sensors, "
    "blinds, fans, automations and scenes, energy and solar, cameras, robot vacuums, "
    "irrigation, the alarm and water heater, people and notifications. Entity ids "
    "look like light.kitchen_ceiling; list tools return the ids."
)
