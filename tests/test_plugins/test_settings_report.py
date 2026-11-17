"""A trusted plugin's settings, as a script run reports them."""

from __future__ import annotations

from clio_agent.plugins.settings_report import describe, plugin_settings

SERVERS = {
    "exa": {
        "command": "./exa",
        "env": {
            "EXA_API_KEY": "${EXA_API_KEY}",
            "EXA_URL": "${EXA_URL:-https://api.exa.ai}",
            "ROOT": "${PLUGIN_ROOT}/data",
            "PROVENANCE": "${CLIO_PROVENANCE_CONFIG}",
        },
    },
    "weather": {"spec": {"url": "${WEATHER_URL}", "headers": {"Auth": "Bearer ${WEATHER_TOKEN}"}}},
}


def test_each_variable_is_set_defaulted_or_empty_and_names_how_to_set_it() -> None:
    rows = plugin_settings(SERVERS, {"WEATHER_TOKEN": "secret-value"})
    assert [(row["name"], row["state"]) for row in rows] == [
        ("EXA_API_KEY", "empty"),
        ("EXA_URL", "default"),
        ("WEATHER_URL", "empty"),
        ("WEATHER_TOKEN", "set"),
    ]
    lines = [describe(row) for row in rows]
    assert lines[0] == "EXA_API_KEY empty (set EXA_API_KEY)"
    assert lines[1] == "EXA_URL default https://api.exa.ai (set EXA_URL to change it)"
    assert lines[3] == "WEATHER_TOKEN set"
    assert "secret-value" not in " ".join(lines)  # an environment value is never shown


def test_values_clio_supplies_are_not_the_persons_to_set() -> None:
    names = {row["name"] for row in plugin_settings(SERVERS, {})}
    assert not {name for name in names if name.startswith(("PLUGIN_", "CLIO_"))}
