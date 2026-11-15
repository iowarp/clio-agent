"""A connector's plugin reaches a conversation only when the person adds it (CN10 *Amended*).

Enabling installs the connector and lists its plugin; it adds nothing to an agent. A
conversation's delta adds it; a connector turned off while a delta names it drops out
with the typed reason ``connector_off`` and comes back when it is on again.
"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from clio_agent.connectors import state
from clio_agent.gact import connector_follow, connector_plugins
from clio_agent.gact.agents import resolution as session_agents
from clio_agent.plugins.resolve import Delta
from clio_agent.plugins.service import PluginService

CARRIER = "clio-connectors/connector-fixture-plain"


@pytest.fixture
def service(fixture_catalog: Path) -> PluginService:
    return PluginService()


def _servers(resolution: Any) -> set[str]:
    return set(resolution.mcp_servers)


def test_enabling_adds_nothing_to_the_agent_and_lists_the_carrier(service: PluginService) -> None:
    before = service.resolve("clio/standard", pin=False)
    state.update("fixture-plain", enabled=True)
    after = service.resolve("clio/standard", pin=False)
    assert after.plugin_ids == before.plugin_ids and "fixture-plain" not in _servers(after)
    assert after.digest == before.digest
    rows = connector_plugins.catalog_rows(
        SimpleNamespace(state=SimpleNamespace()),
        service.store.records(),
        lambda record, trusted: {"id": record.id, "follows": record.follows, "trusted": trusted},
        service.store.trusted,
    )
    carrier = next(row for row in rows if row["id"] == "connector-fixture-plain")
    assert carrier["trusted"] is True
    assert carrier["connector"]["id"] == "fixture-plain"
    assert carrier["connector"]["enabled"] is True
    assert carrier["connector"]["name"] == "Fixture Plain"
    state.update("fixture-plain", enabled=False)
    assert connector_plugins.carrier_records(service.store.records()) == []


def test_a_delta_adds_the_plugin_and_an_off_connector_drops_it_with_a_reason(
    service: PluginService,
) -> None:
    state.update("fixture-plain", enabled=True)
    added = service.resolve("clio/standard", delta=Delta((CARRIER,)), pin=False)
    row = next(r for r in added.plugins if r["id"] == "connector-fixture-plain")
    assert row["follows"] == "fixture-plain" and "fixture-plain" in _servers(added)
    state.update("fixture-plain", enabled=False)
    off = service.resolve("clio/standard", delta=Delta((CARRIER,)), pin=False)
    assert "connector-fixture-plain" not in off.plugin_ids
    assert {
        "id": "connector-fixture-plain",
        "reason": "connector_off",
        "because": "fixture-plain",
    } in (off.removed)
    assert off.digest != added.digest
    # The same agent with no delta has no removed row.
    assert service.resolve("clio/standard", pin=False).digest != off.digest


class _Sessions:
    def __init__(self) -> None:
        self.rows = {"sess_a": SimpleNamespace(metadata={}), "sess_b": SimpleNamespace(metadata={})}

    def get(self, sid: str) -> Any:
        return self.rows.get(sid)

    def update(self, sid: str, *, metadata_patch: dict, agent: Any = None) -> None:
        self.rows[sid].metadata = {**self.rows[sid].metadata, **metadata_patch}


def test_adding_to_one_conversation_leaves_the_other_and_follows_off_and_on(
    service: PluginService, monkeypatch: pytest.MonkeyPatch
) -> None:
    app = SimpleNamespace(
        state=SimpleNamespace(
            plugin_service=service, sessions=_Sessions(), bus=None, session_defaults=None
        )
    )
    warmed: list[str] = []
    monkeypatch.setattr(
        "clio_agent.gact.session_warmup.start_session_warmup",
        lambda _app, sid, trigger: warmed.append(f"{sid}:{trigger}"),
    )
    state.update("fixture-plain", enabled=True)
    first = session_agents.pin_session_agent(app, "sess_a")
    session_agents.pin_session_agent(app, "sess_b")
    assert connector_follow.in_conversation(app, "sess_a", "fixture-plain") == []
    plugins = connector_follow.add_to_conversation(app, "sess_a", "fixture-plain")
    assert plugins == ["connector-fixture-plain"]
    metadata = app.state.sessions.get("sess_a").metadata
    assert metadata["agent_delta"] == {"add": [CARRIER], "remove": []}
    assert metadata["agent_prompt_basis"] == first.digest  # a note-mode change
    assert warmed == ["sess_a:agent_changed"]
    assert connector_follow.add_to_conversation(app, "sess_a", "fixture-plain") == plugins
    assert connector_follow.in_conversation(app, "sess_b", "fixture-plain") == []
    # Turned off: the open conversation that has it is pinned again without it.
    state.update("fixture-plain", enabled=False)
    monkeypatch.setattr(connector_follow, "open_session_ids", lambda _app: ["sess_a", "sess_b"])
    assert connector_follow.follow_in_open_sessions(app) == ["sess_a"]
    off = session_agents.session_resolution(app, "sess_a")
    assert "connector-fixture-plain" not in off.plugin_ids
    assert off.removed[-1]["reason"] == "connector_off"
    state.update("fixture-plain", enabled=True)
    back = session_agents.session_resolution(app, "sess_a")
    assert "connector-fixture-plain" in back.plugin_ids
    assert connector_follow.follow_in_open_sessions(app) == []


def test_a_subagent_conversation_cannot_take_the_plugin(
    service: PluginService, monkeypatch: pytest.MonkeyPatch
) -> None:
    sessions = _Sessions()
    app = SimpleNamespace(state=SimpleNamespace(plugin_service=service, sessions=sessions))
    state.update("fixture-plain", enabled=True)
    session_agents.pin_session_agent(app, "sess_a")
    sessions.rows["sess_a"].metadata["agent_subagent"] = "helper"
    with pytest.raises(connector_follow.ConnectorAddError) as refused:
        connector_follow.add_to_conversation(app, "sess_a", "fixture-plain")
    assert refused.value.reason == "subagent_plugins_fixed"
