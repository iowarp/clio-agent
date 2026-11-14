"""Connector values per conversation (CV-D1 to CV-D6).

The overlay is checked on every read (a client can patch session metadata), filled
into the conversation's server declarations, keys the workspace fleet, and a value
change rebinds only the fleets whose connection really changes.
"""

from __future__ import annotations

import asyncio
import threading
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from clio_agent.connectors import entries, materialize, overlay, secrets, state
from clio_agent.gact import connector_values
from clio_agent.gact import connectors_service as service
from clio_agent.plugins import servers
from clio_agent.tools import fleet_key
from tests.test_connectors.conftest import FakeSessions

PLUGIN_ROW = {
    "fixture-secret": {
        "spec": {"type": "clio-connection", "connection": "fixture-secret"},
        "root": "/plugins/fixture-secret-skills",
        "plugin": "fixture-secret-skills",
    }
}


def _app() -> Any:
    return SimpleNamespace(state=SimpleNamespace(bus=None, sessions=FakeSessions(), agent=None))


def _connections() -> dict[str, Any]:
    found = state.all_entries()["fixture-secret"]
    return {"fixture-secret": materialize.declaration(found)}


@pytest.fixture
def plugin_data(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setattr(servers, "plugin_data_dir", lambda plugin: tmp_path / plugin)


# --------------------------------------------------------------- the overlay


def test_a_client_patched_overlay_is_checked_on_every_read(
    fixture_catalog: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    events: list[str] = []
    monkeypatch.setattr(
        "clio_agent.runtime.trace.event", lambda _c, fmt, *a: events.append(fmt % a)
    )
    app = _app()
    # What PATCH /v1/sessions/{sid} could store: the session's metadata, unchecked.
    app.state.sessions.update(
        "sess_a",
        metadata_patch={
            overlay.KEY: {
                "fixture-secret": {
                    "where": "node08",
                    "token": "a secret is never per conversation",
                },
                "fixture-signin": {
                    "address": "-oProxyCommand=touch /tmp/pwned",
                    "account": "me",
                },
                "fixture-plain": {"anything": "x"},
                "no-such-connector": {"x": "y"},
            }
        },
    )
    assert connector_values.session_overlay(app, "sess_a") == {
        "fixture-secret": {"where": "node08"}
    }
    reasons = " ".join(events)
    assert "secret_not_per_conversation" in reasons
    assert "sign-in_not_per_conversation" in reasons
    assert "an_address_cannot_start_with_'-'" in reasons
    assert "unknown_need" in reasons and "unknown_connector" in reasons
    assert "touch /tmp" not in reasons and "never per conversation" not in reasons  # no values
    for bad in ("a\nb", "${need.token}", "x" * 5000, "", 7):
        app.state.sessions.update(
            "sess_b", metadata_patch={overlay.KEY: {"fixture-secret": {"where": bad}}}
        )
        assert connector_values.session_overlay(app, "sess_b") == {}, bad
    app.state.sessions.update("sess_c", metadata_patch={overlay.KEY: ["not", "a", "mapping"]})
    assert connector_values.session_overlay(app, "sess_c") == {}


def test_the_same_check_refuses_an_option_injection_from_the_card(fixture_catalog: Path) -> None:
    found = entries.load_catalog().entries["fixture-secret"]
    with pytest.raises(service.ConnectorError) as dash:
        service.check_values(found, {"where": "-oProxyCommand=x"}, by_agent=False)
    assert dash.value.code == "connector_value_invalid"


def test_the_overlay_digest_is_stable_and_empty_for_no_values() -> None:
    assert overlay.digest({}) == overlay.digest({"a": {}}) == ""
    one = overlay.digest({"a": {"x": "1"}, "b": {"y": "2"}})
    assert one == overlay.digest({"b": {"y": "2"}, "a": {"x": "1"}}) and len(one) == 12
    assert one != overlay.digest({"a": {"x": "2"}, "b": {"y": "2"}})


# -------------------------------------------------------------- substitution


def test_the_saved_connection_is_a_template(fixture_catalog: Path) -> None:
    found = entries.load_catalog().entries["fixture-secret"]
    declared = materialize.declaration(found)
    assert declared["env"] == {"FIXTURE_TOKEN": "${need.token}", "FIXTURE_WHERE": "${need.where}"}
    state.update("fixture-secret", values={"where": "default-host"})
    filled = materialize.fill_declaration(declared, {"where": "node08"})
    assert filled["env"] == {"FIXTURE_TOKEN": "${need.token}", "FIXTURE_WHERE": "node08"}
    assert materialize.fill_declaration(declared)["env"]["FIXTURE_WHERE"] == "default-host"
    # A connection an older CLIO saved with the value written in follows the conversation.
    baked = {**declared, "env": {"FIXTURE_TOKEN": "${need.token}", "FIXTURE_WHERE": "old"}}
    rebuilt = materialize.fill_declaration(baked, {"where": "node08"}, from_entry=True)
    assert rebuilt["env"]["FIXTURE_WHERE"] == "node08"


def test_each_conversation_gets_its_values_and_its_own_server_set(
    fixture_catalog: Path, plugin_data: None
) -> None:
    state.update("fixture-secret", values={"where": "this-machine"})
    connections = _connections()
    plain = servers.server_declarations(PLUGIN_ROW, connections=connections)
    mine = servers.server_declarations(
        PLUGIN_ROW, connections=connections, connector_values={"fixture-secret": {"where": "n8"}}
    )
    env = lambda d: d["fixture-secret-skills"]["fixture-secret"]["env"]  # noqa: E731
    assert env(plain)["FIXTURE_WHERE"] == "this-machine"
    assert env(mine)["FIXTURE_WHERE"] == "n8"
    assert env(mine)["FIXTURE_TOKEN"] == "${need.token}"  # the secret is filled at start only

    a = servers.register_server_set(PLUGIN_ROW, connections=connections)
    b = servers.register_server_set(
        PLUGIN_ROW, connections=connections, connector_values={"fixture-secret": {"where": "n8"}}
    )
    c = servers.register_server_set(
        PLUGIN_ROW, connections=connections, connector_values={"fixture-secret": {"where": "n8"}}
    )
    assert servers.server_set_variant(a) == "" and servers.VARIANT_MARK not in a
    assert b == c and servers.server_set_variant(b)
    assert servers.variant_overlay(servers.server_set_variant(b)) == {
        "fixture-secret": {"where": "n8"}
    }
    assert env(servers.server_set_declarations(b))["FIXTURE_WHERE"] == "n8"


def test_a_spec_parsed_outside_a_conversation_takes_the_defaults(fixture_catalog: Path) -> None:
    from clio_agent.tools.mcp_config import spec_from_declaration

    found = entries.load_catalog().entries["fixture-secret"]
    state.update("fixture-secret", values={"where": "default-host"})
    secrets.save("fixture-secret", {"token": "t0k"})
    spec = spec_from_declaration("fixture-secret", materialize.declaration(found))
    assert spec.usable and spec.env["FIXTURE_WHERE"] == "default-host"
    assert spec.env["FIXTURE_TOKEN"] == "t0k"


# ---------------------------------------------------------------- the fleets


def test_fleet_keys() -> None:
    assert fleet_key.fleet_key("/ws", "") == "/ws"
    key = fleet_key.fleet_key("/ws", "abc")
    assert fleet_key.fleet_root(key) == "/ws" and fleet_key.fleet_variant(key) == "abc"
    assert fleet_key.keys_at(["/ws", key, "/other", "/ws2|cv=x"], "/ws") == ["/ws", key]


def test_conversations_with_their_own_values_get_their_own_fleet(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from clio_agent.agent import ClioAgent
    from clio_agent.tools.execution import tool_server_set_context, tool_workspace_context

    agent = ClioAgent.__new__(ClioAgent)
    agent.tool_executor = object()
    agent._workspace_tool_executors = {}
    agent._tool_definitions = {}
    gateways: list[tuple[str, str]] = []

    def build(*, cwd: str = "", server_set: str = "", **_: Any) -> Any:
        gateways.append((cwd, server_set))
        return SimpleNamespace(server_set=server_set)

    monkeypatch.setattr(agent, "_build_tool_gateway", build)
    for owner in ("clio_agent.tools.workspace_fleet", "clio_agent.tools.fleet_server_merge"):
        monkeypatch.setattr(f"{owner}.namespace_specs", lambda _g: {})
        monkeypatch.setattr(f"{owner}.namespace_proxies", lambda _g: {})
    monkeypatch.setattr(
        "clio_agent.tools.workspace_fleet.create_sync_tool_executor",
        lambda gateway, **kw: SimpleNamespace(closed=False, server_id=kw["server_id"]),
    )

    def resolve(server_set: str) -> Any:
        with tool_workspace_context("/ws/paper"), tool_server_set_context(server_set):
            return agent._active_tool_executor()

    plain = resolve("servers-aaaa")
    also_plain = resolve("servers-aaaa")
    node08 = resolve("servers-bbbb~cv-0123456789ab")
    same_values = resolve("servers-bbbb~cv-0123456789ab")
    assert plain is also_plain and node08 is same_values and node08 is not plain
    assert sorted(agent._workspace_tool_executors) == ["/ws/paper", "/ws/paper|cv=0123456789ab"]
    assert node08.server_id == "gateway:/ws/paper|cv=0123456789ab"
    assert {cwd for cwd, _set in gateways} == {"/ws/paper"}  # servers start in the workspace


def test_a_default_change_restarts_only_the_fleets_that_follow_it(
    fixture_catalog: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from clio_agent.gact import connector_rebind
    from clio_agent.tools import gateway, mcp_discovery
    from clio_agent.tools.mcp_config import spec_from_declaration

    found = entries.load_catalog().entries["fixture-secret"]
    template = materialize.declaration(found)
    monkeypatch.setattr(mcp_discovery, "ensure_namespace", lambda n, s: {f"{n}_check": {}})
    monkeypatch.setattr(gateway, "build_gateway", lambda *_a, **_k: object())
    monkeypatch.setattr(gateway, "namespace_proxies", lambda _g: {})
    monkeypatch.setattr(gateway, "namespace_direct_factories", lambda _g: {})

    def fleet(values: dict[str, str]) -> SimpleNamespace:
        spec = spec_from_declaration(
            "fixture-secret", materialize.fill_declaration(template, values), source="connector"
        )
        resets: list[str] = []
        return SimpleNamespace(
            _clio_namespace_specs={"fixture-secret": spec},
            _clio_connector_values={"fixture-secret": values} if values else {},
            reset_namespace=resets.append,
            merge_namespace_tools=lambda _n, _t: None,
            resets=resets,
        )

    plain, node08 = fleet({}), fleet({"where": "node08"})
    executors = {"/ws": plain, "/ws|cv=abc": node08}
    agent = SimpleNamespace(
        tool_executor=None, _workspace_state=lambda: (threading.Lock(), executors, {})
    )
    state.update("fixture-secret", values={"where": "new-default"})
    touched = connector_rebind.rebind_namespace(agent, "fixture-secret", template)
    assert touched == ["/ws"]  # the conversation that set its own address kept its server
    assert plain.resets == ["fixture-secret"] and node08.resets == []
    assert plain._clio_namespace_specs["fixture-secret"].env["FIXTURE_WHERE"] == "new-default"
    assert node08._clio_namespace_specs["fixture-secret"].env["FIXTURE_WHERE"] == "node08"


def test_a_turn_lease_on_the_workspace_protects_every_fleet_there() -> None:
    from clio_agent.tools.reaper import WorkspaceExecutorReaper

    class Fleet:
        busy = False
        closed = False

        def idle_for(self) -> float:
            return 10_000.0

        def close(self) -> None:
            self.closed = True

    lock = threading.Lock()
    registry = {"/ws": Fleet(), "/ws|cv=abc": Fleet(), "/other": Fleet()}
    leases = {"/ws": 1}
    closed_channels: list[str] = []
    reaper = WorkspaceExecutorReaper(
        registry, lock, leases=leases, ttl_s=1.0, channel_closer=closed_channels.append
    )
    assert reaper.reap_once() == ["/other"]
    assert sorted(registry) == ["/ws", "/ws|cv=abc"]
    leases.clear()
    registry["/ws"].busy = True  # one fleet of the workspace still in use
    assert reaper.reap_once() == ["/ws|cv=abc"]
    assert closed_channels == ["/other"]  # /ws's channels stay while its plain fleet runs


# ---------------------------------------------------------- setting values


def test_a_conversation_value_is_not_a_default_and_save_as_default_is(
    fixture_catalog: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    app = _app()
    state.update("fixture-secret", enabled=True, values={"where": "this-machine"})
    moved: list[str] = []
    applied: list[str] = []
    monkeypatch.setattr(
        connector_values,
        "move_session_fleet",
        lambda _app, sid, found, move_turn: moved.append(sid)
        or {"status": "ready", "tool_count": 3, "reason": None},
    )

    async def apply(_app: Any, found: Any) -> dict:
        applied.append(found.id)
        return {}

    monkeypatch.setattr(service, "apply", apply)
    row = asyncio.run(
        connector_values.set_session_values(
            app, "sess_a", "fixture-secret", {"where": "node08"}, by_agent=True
        )
    )
    assert state.choice("fixture-secret").values == {"where": "this-machine"}
    assert connector_values.session_overlay(app, "sess_a") == {
        "fixture-secret": {"where": "node08"}
    }
    assert connector_values.session_overlay(app, "sess_b") == {}
    where = next(n for n in row["needs"] if n["key"] == "where")
    assert where["value"] == "node08" and where["default_value"] == "this-machine"
    assert where["conversation"] is True and row["moved"] is True and row["tool_count"] == 3
    assert moved == ["sess_a"] and applied == []

    with pytest.raises(service.ConnectorError) as secret:
        asyncio.run(
            connector_values.set_session_values(
                app, "sess_a", "fixture-secret", {"token": "x"}, by_agent=True
            )
        )
    assert secret.value.code == "connector_secret_not_agent_settable"

    row = asyncio.run(
        connector_values.set_session_values(
            app,
            "sess_a",
            "fixture-secret",
            {"where": "ares"},
            by_agent=False,
            save_as_default=["where"],
        )
    )
    assert state.choice("fixture-secret").values == {"where": "ares"}
    assert connector_values.session_overlay(app, "sess_a") == {}  # follows the default now
    assert applied == ["fixture-secret"] and row["moved"] is True

    with pytest.raises(service.ConnectorError) as unknown:
        connector_values.default_keys({"save_as_default": ["token"]}, {"where": "x"})
    assert unknown.value.code == "connector_default_unknown"
    with pytest.raises(service.ConnectorError) as invalid:
        connector_values.default_keys({"save_as_default": "where"}, {"where": "x"})
    assert invalid.value.code == "connector_default_invalid"


def test_a_running_turn_takes_a_moved_namespace_from_the_new_fleet() -> None:
    import dspy

    from clio_agent.gact.agents.live_tool_refresh import LiveToolRefresher

    def tool(name: str, answer: str) -> Any:
        def run() -> str:
            return answer

        return dspy.Tool(run, name=name, desc=name)

    class Fleet:
        def __init__(self, answer: str) -> None:
            self.tools_revision = 1
            self.answer = answer

        def refresh_changed_namespaces(self) -> dict:
            return {}

        def to_dspy_tools(self) -> list[Any]:
            return [tool("pv_open", self.answer), tool("docs_find", self.answer)]

    shared, own = Fleet("shared"), Fleet("own")
    refresher = LiveToolRefresher(shared, {"pv", "docs"}, wrap=lambda tools: tools)
    tools: dict[str, Any] = {}
    refresher._revision = -1
    refresher(tools)
    assert tools["pv_open"].func() == "shared"
    refresher.move("pv", own)
    assert refresher(tools) is True
    assert tools["pv_open"].func() == "own" and tools["docs_find"].func() == "shared"
    assert refresher(tools) is False  # nothing new on either fleet


def test_a_sign_in_is_stored_under_the_address_in_use(fixture_catalog: Path) -> None:
    """FQ-F1: the saved connection is a template, so signing in from it stored the token
    under "${need.address}" while the connector looked under its address: AQ3 stayed
    "Needs a sign-in" after a sign-in that connected."""

    found = entries.load_catalog().entries["fixture-signin"]
    state.update("fixture-signin", values={"address": "http://127.0.0.1:19101/mcp"})
    declared = materialize.declaration(found)
    assert "${need.address}" in str(declared.get("url"))
    used = materialize.in_use(declared)
    assert used["url"] == "http://127.0.0.1:19101/mcp"
    choice = state.choice("fixture-signin")
    checked = service._sign_in_storage(found, choice.values)
    assert checked.server_url == used["url"]
    # A connection that is not a connector's is used as saved.
    plain = {"url": "https://example.org/mcp"}
    assert materialize.in_use(plain) == plain
