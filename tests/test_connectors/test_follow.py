"""A plugin follows its connector into the person's additions (CN9)."""

from __future__ import annotations

from pathlib import Path

from clio_agent.connectors import follow, state
from clio_agent.plugins.records import PluginRecord


def _record(plugin_id: str, follows: str = "") -> PluginRecord:
    return PluginRecord(
        id=plugin_id,
        version="1.0.0",
        description="",
        kind="plugin",
        format="agent-plugins",
        root="/tmp",
        follows=follows,
    )


def test_enabled_connectors_add_their_following_plugin_or_a_built_carrier(
    fixture_catalog: Path,
) -> None:
    records = [_record("fixture-secret-skills", "fixture-secret"), _record("other")]
    assert follow.additions(records, ("clio/standard",)) == ([], [])
    state.update("fixture-secret", enabled=True, scope="all")
    state.update("fixture-plain", enabled=True, scope={"agents": ["clio/other"]})
    refs, built = follow.additions(records, ("clio/standard",))
    assert refs == ["fixture-secret-skills"] and built == []
    refs, built = follow.additions(records, ("clio/other",))
    assert refs == ["connector-fixture-plain", "fixture-secret-skills"]
    assert [record.id for record in built] == ["connector-fixture-plain"]
    carrier = built[0]
    assert carrier.follows == "fixture-plain"
    assert carrier.mcp_servers == {
        "fixture-plain": {"type": "clio-connection", "connection": "fixture-plain"}
    }
    state.update("fixture-secret", enabled=False)
    assert follow.applicable(("clio/standard",)) == []
