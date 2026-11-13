"""A connector's plugin is listed while it is on and added per conversation (CN9, CN10)."""

from __future__ import annotations

from pathlib import Path

from clio_agent.connectors import follow, state
from clio_agent.plugins.records import PluginRecord
from clio_agent.plugins.resolve import Delta


def _record(plugin_id: str, follows: str = "") -> PluginRecord:
    return PluginRecord(
        id=plugin_id,
        version="1.0.0",
        description="",
        kind="plugin",
        format="agent-plugins",
        root="/tmp",
        marketplace="m",
        follows=follows,
    )


RECORDS = [_record("fixture-secret-skills", "fixture-secret"), _record("other")]


def test_every_enabled_connector_has_a_carrier_and_the_listing_hides_followed_ones(
    fixture_catalog: Path,
) -> None:
    assert follow.carriers(RECORDS) == [] and follow.enabled_ids() == set()
    state.update("fixture-secret", enabled=True)
    state.update("fixture-plain", enabled=True)
    assert follow.enabled_ids() == {"fixture-plain", "fixture-secret"}
    built = follow.carriers(RECORDS)
    assert [r.id for r in built] == ["connector-fixture-plain", "connector-fixture-secret"]
    assert built[0].follows == "fixture-plain"
    assert built[0].mcp_servers == {
        "fixture-plain": {"type": "clio-connection", "connection": "fixture-plain"}
    }
    # The picker lists the installed following plugin instead of the secret's carrier.
    assert [r.id for r in follow.listed_carriers(RECORDS)] == ["connector-fixture-plain"]
    assert follow.plugin_refs("fixture-secret", RECORDS) == ["m/fixture-secret-skills"]
    assert follow.plugin_refs("fixture-plain", RECORDS) == [
        "clio-connectors/connector-fixture-plain"
    ]


def test_connector_of_reads_follows_and_carrier_ids(fixture_catalog: Path) -> None:
    assert follow.connector_of("fixture-secret-skills", RECORDS) == "fixture-secret"
    assert follow.connector_of("connector-fixture-plain", RECORDS) == "fixture-plain"
    assert follow.connector_of("other", RECORDS) == ""
    assert follow.connector_of("unrelated", RECORDS) == ""
    assert follow.follows_flag({"follows": "x"}) == "x" and follow.follows_flag(RECORDS[1]) == ""


def test_a_delta_drops_the_plugin_of_a_connector_that_is_off_with_a_typed_reason(
    fixture_catalog: Path,
) -> None:
    delta = Delta(("m/fixture-secret-skills", "connector-fixture-plain", "other", "connector-gone"))
    kept, dropped = follow.drop_off(delta, RECORDS)
    assert kept.add == ("other",)
    assert dropped == [
        {"id": "fixture-secret-skills", "reason": "connector_off", "because": "fixture-secret"},
        {"id": "connector-fixture-plain", "reason": "connector_off", "because": "fixture-plain"},
        {"id": "connector-gone", "reason": "connector_not_found", "because": "gone"},
    ]
    state.update("fixture-plain", enabled=True)
    kept, dropped = follow.drop_off(delta, RECORDS)
    assert kept.add == ("connector-fixture-plain", "other")
    assert [row["id"] for row in dropped] == ["fixture-secret-skills", "connector-gone"]
    assert follow.drop_off(Delta(), RECORDS) == (Delta(), [])
    assert follow.drop_off(Delta(("other",)), RECORDS) == (Delta(("other",)), [])


def test_drifted_says_when_a_pin_no_longer_matches_the_connectors(fixture_catalog: Path) -> None:
    class Pin:
        def __init__(self, plugins: list[dict], removed: list[dict]) -> None:
            self.plugins, self.removed = plugins, removed

    plain = {"id": "connector-fixture-plain", "follows": "fixture-plain"}
    dropped = {
        "id": "connector-fixture-plain",
        "reason": "connector_off",
        "because": "fixture-plain",
    }
    assert not follow.drifted(Pin([{"id": "other", "follows": ""}], []))
    assert follow.drifted(Pin([plain], []))  # its connector is off now
    assert not follow.drifted(Pin([], [dropped]))
    state.update("fixture-plain", enabled=True)
    assert not follow.drifted(Pin([plain], []))
    assert follow.drifted(Pin([], [dropped]))  # it can come back


def test_enabling_keeps_no_scope_and_drops_an_older_one(fixture_catalog: Path) -> None:
    state.update("fixture-plain", enabled=True)
    assert state.choice("fixture-plain") == state.Choice(enabled=True)
    raw = state._read()["connectors"]["fixture-plain"]
    raw["scope"] = {"agents": ["clio/standard"]}
    state._write({"connectors": {"fixture-plain": raw}, "custom": {}})
    state.update("fixture-plain", values={"x": "y"})
    assert "scope" not in state._read()["connectors"]["fixture-plain"]
