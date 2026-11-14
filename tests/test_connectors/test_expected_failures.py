"""The connector and app paths catch only the failures they expect (GT-O1).

Each best-effort path names the failure it expects and reports it typed (in the result,
the row or the brief); anything else is a fault and raises instead of being swallowed.
"""

from __future__ import annotations

import asyncio
import threading
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from clio_agent.connectors import entries, materialize, state
from clio_agent.gact import connectors_service as service


def _app() -> Any:
    from tests.test_connectors.conftest import FakeSessions

    return SimpleNamespace(state=SimpleNamespace(bus=None, sessions=FakeSessions(), agent=None))


def _boom(error: BaseException) -> Any:
    def raise_it(*_a: Any, **_k: Any) -> Any:
        raise error

    return raise_it


# ------------------------------------------------- connector_offer.join_running_turn


def _turn_without_executor(monkeypatch: pytest.MonkeyPatch) -> None:
    from clio_agent.gact.agents import clio_react
    from clio_agent.gact.mcp_app_tools import AppToolRefresher

    program = SimpleNamespace(_clio_tool_refresh=AppToolRefresher(None))
    monkeypatch.setattr(clio_react, "active_loop", lambda: SimpleNamespace(agent=program))


@pytest.mark.parametrize("error", [RuntimeError("prepare failed"), OSError("no folder")])
def test_a_fleet_that_cannot_be_built_now_is_a_typed_join_reason(
    fixture_catalog: Path, monkeypatch: pytest.MonkeyPatch, error: Exception
) -> None:
    from clio_agent.gact import connector_offer as offer

    _turn_without_executor(monkeypatch)
    monkeypatch.setattr(
        "clio_agent.gact.agents.builders._active_base_agent_tool_executor", _boom(error)
    )
    assert offer.join_running_turn(_app(), "fixture-plain") == (
        [],
        "connector_join_fleet_unavailable",
    )


def test_a_fault_resolving_the_fleet_raises(
    fixture_catalog: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from clio_agent.gact import connector_offer as offer

    _turn_without_executor(monkeypatch)
    monkeypatch.setattr(
        "clio_agent.gact.agents.builders._active_base_agent_tool_executor",
        _boom(AttributeError("bug")),
    )
    with pytest.raises(AttributeError):
        offer.join_running_turn(_app(), "fixture-plain")


# ------------------------------------------------------ connector_plugins.connector_brief


def test_an_unreadable_connector_state_is_named_in_the_brief(
    fixture_catalog: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from clio_agent.gact import connector_plugins

    unreadable = state.ConnectorStateError("could not read connectors.yaml")
    monkeypatch.setattr(service, "row", _boom(unreadable))
    brief = connector_plugins.connector_brief(_app(), "fixture-plain")
    assert brief is not None
    assert (brief["enabled"], brief["status"]) == (False, "unknown")
    assert brief["reason"] == "connector_state_unreadable: could not read connectors.yaml"


def test_a_fault_reading_the_brief_raises(
    fixture_catalog: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from clio_agent.gact import connector_plugins

    monkeypatch.setattr(service, "row", _boom(KeyError("bug")))
    with pytest.raises(KeyError):
        connector_plugins.connector_brief(_app(), "fixture-plain")


# ---------------------------------------------- connector_rebind and connectors_service


def _rebind_agent(monkeypatch: pytest.MonkeyPatch, listing: Any) -> tuple[Any, dict]:
    from clio_agent.tools import gateway, mcp_discovery
    from clio_agent.tools.mcp_config import spec_from_declaration

    found = entries.load_catalog().entries["fixture-secret"]
    declared = materialize.resolved(found, {"where": "node08"})
    current = spec_from_declaration("fixture-secret", declared, source="connector")
    monkeypatch.setattr(mcp_discovery, "ensure_namespace", listing)
    monkeypatch.setattr(gateway, "build_gateway", lambda *_a, **_k: object())
    monkeypatch.setattr(gateway, "namespace_proxies", lambda _g: {})
    monkeypatch.setattr(gateway, "namespace_direct_factories", lambda _g: {})
    workspace = SimpleNamespace(
        _clio_namespace_specs={"fixture-secret": current},
        reset_namespace=lambda _n: None,
        merge_namespace_tools=lambda _n, _tools: pytest.fail("a failed listing merges nothing"),
    )
    agent = SimpleNamespace(
        tool_executor=None, _workspace_state=lambda: (threading.Lock(), {"/w": workspace}, {})
    )
    return agent, declared


def test_a_failed_relist_is_handed_to_the_caller(
    fixture_catalog: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from clio_agent.gact import connector_rebind

    agent, declared = _rebind_agent(monkeypatch, _boom(RuntimeError("server exited")))
    failed: dict[str, str] = {}
    touched = connector_rebind.rebind_namespace(agent, "fixture-secret", declared, failed)
    assert touched == ["/w"]  # the fleet took the new declaration and keeps its last tools
    assert failed == {"/w": "RuntimeError: server exited"}


def test_a_failed_relist_reads_unreachable_in_the_connector_row(
    fixture_catalog: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from clio_agent.gact import connector_follow, connector_rebind
    from clio_agent.gact.routes import mcp_configuration

    found = state.all_entries()["fixture-plain"]
    state.update(found.id, enabled=True)
    app = _app()
    app.state.agent = object()

    async def nothing(*_a: Any, **_k: Any) -> None:
        return None

    def relist(_agent: Any, _ns: str, _decl: Any, failed: dict[str, str]) -> list[str]:
        failed["/w"] = "RuntimeError: server exited"
        return ["/w"]

    monkeypatch.setattr(service, "save_connection", lambda _f, _c: {"command": "fixture"})
    monkeypatch.setattr(mcp_configuration, "_refresh_agent_tool_gateway", nothing)
    monkeypatch.setattr(service, "probe", nothing)
    monkeypatch.setattr(connector_rebind, "rebind_namespace", relist)
    monkeypatch.setattr(connector_follow, "follow_in_open_sessions", lambda _app: [])
    row = asyncio.run(service.apply(app, found))
    assert row["state"]["status"] == "unreachable"
    assert "server exited" in row["state"]["reason"]


# --------------------------------------------------- connector_values.session_fleet_variant


def test_an_unresolvable_agent_reads_the_plain_fleet(monkeypatch: pytest.MonkeyPatch) -> None:
    from clio_agent.gact import connector_values
    from clio_agent.gact.agents import resolution

    monkeypatch.setattr(connector_values, "session_overlay", lambda _a, _s: {"x": {"k": "v"}})
    monkeypatch.setattr(
        resolution,
        "session_server_set",
        _boom(resolution.SessionAgentUnavailable("", "mcp_connection_unavailable", [])),
    )
    assert connector_values.session_fleet_variant(_app(), "sess_a") == ""


def test_a_fault_reading_the_fleet_variant_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    from clio_agent.gact import connector_values
    from clio_agent.gact.agents import resolution

    monkeypatch.setattr(connector_values, "session_overlay", lambda _a, _s: {"x": {"k": "v"}})
    monkeypatch.setattr(resolution, "session_server_set", _boom(TypeError("bug")))
    with pytest.raises(TypeError):
        connector_values.session_fleet_variant(_app(), "sess_a")


# ------------------------------------------------------------- connectors_service._records


def test_an_unreadable_plugin_state_leaves_the_row_its_own_plugin() -> None:
    from clio_agent.plugins.store import StoreError

    store = SimpleNamespace(records=_boom(StoreError("plugin_state_unreadable", "bad json")))
    app = SimpleNamespace(state=SimpleNamespace(plugin_service=SimpleNamespace(store=store)))
    assert service._records(app) == []


def test_a_fault_reading_plugin_records_raises() -> None:
    store = SimpleNamespace(records=_boom(AttributeError("bug")))
    app = SimpleNamespace(state=SimpleNamespace(plugin_service=SimpleNamespace(store=store)))
    with pytest.raises(AttributeError):
        service._records(app)


# ---------------------------------------------------------- plugin_offer.offerable_facts


def test_unreadable_stores_give_no_offer_facts(monkeypatch: pytest.MonkeyPatch) -> None:
    from clio_agent.gact import plugin_offer
    from clio_agent.plugins.store import StoreError

    monkeypatch.setattr(
        plugin_offer, "offerable", _boom(StoreError("plugin_state_unreadable", "x"))
    )
    assert plugin_offer.offerable_facts(_app(), "sess_a") == ([], 0)
    monkeypatch.setattr(plugin_offer, "offerable", _boom(state.ConnectorStateError("bad")))
    assert plugin_offer.offerable_facts(_app(), "sess_a") == ([], 0)


def test_a_fault_listing_offer_facts_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    from clio_agent.gact import plugin_offer

    monkeypatch.setattr(plugin_offer, "offerable", _boom(KeyError("bug")))
    with pytest.raises(KeyError):
        plugin_offer.offerable_facts(_app(), "sess_a")


# ------------------------------------------------- tools.fleet_close_listeners


def test_a_listener_whose_loop_closed_does_not_break_the_close() -> None:
    from clio_agent.tools import fleet_close_listeners as listeners

    heard: list[str] = []
    remove_closed = listeners.listen(_boom(RuntimeError("Event loop is closed")))
    remove_ok = listeners.listen(lambda root, _proxies, _reason: heard.append(root))
    try:
        listeners.proxies_dropped("/w", [object()], "reaped")
    finally:
        remove_closed()
        remove_ok()
    assert heard == ["/w"]


def test_a_faulty_listener_raises_from_the_close() -> None:
    from clio_agent.tools import fleet_close_listeners as listeners

    remove = listeners.listen(_boom(TypeError("bug")))
    try:
        with pytest.raises(TypeError):
            listeners.proxies_dropped("/w", [object()], "reaped")
    finally:
        remove()


# --------------------------------------------------- mcp_app_downloads.save_app_download


def _download(monkeypatch: pytest.MonkeyPatch, tmp_path: Path, mint_error: Exception) -> dict:
    from clio_agent.gact import mcp_app_downloads
    from clio_agent.gact.artifacts import minting

    monkeypatch.setattr(minting, "_session_workspace_id", lambda _app, _sid: "ws")
    monkeypatch.setattr(minting, "_workspace_root", lambda _app, _ws: tmp_path)
    monkeypatch.setattr(mcp_app_downloads, "_mint", _boom(mint_error))
    contents = [{"type": "resource", "resource": {"uri": "file:///a.txt", "text": "hi"}}]
    record = SimpleNamespace(session_id="sess_a")
    return mcp_app_downloads.save_app_download(_app(), record, {"contents": contents})


def test_a_download_whose_artifact_was_not_registered_says_so(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    saved = _download(monkeypatch, tmp_path, RuntimeError("registry unavailable"))
    [row] = saved["files"]
    assert (row["artifact_id"], row["artifact_skipped"]) == ("", "artifact_mint_failed")
    assert (tmp_path / row["path"]).read_text() == "hi"


def test_a_fault_registering_the_download_raises(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    with pytest.raises(TypeError):
        _download(monkeypatch, tmp_path, TypeError("bug"))
