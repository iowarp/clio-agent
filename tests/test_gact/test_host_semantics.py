"""Host semantics for plugin MCP servers: surfaces, actions, tabs and url trust (HS1 to HS5)."""

from __future__ import annotations

import asyncio
import importlib.util
import json
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from clio_agent.gact import plugin_tabs as tabs_module
from clio_agent.gact import server_actions
from clio_agent.gact.a2ui_catalogs.builtin import basic_catalog_id
from clio_agent.gact.a2ui_catalogs.registry import CatalogRegistry
from clio_agent.gact.elicitation_schema import (
    URL_TRUST_CONFIGURED,
    URL_TRUST_PLUGIN_LOOPBACK,
    URL_TRUST_PLUGIN_ORIGIN,
    build_url_metadata,
    classify_url_trust,
)
from clio_agent.gact.server_actions import (
    ServerActionError,
    ServerActionOutcome,
    login_provider,
    resolve_server_tool,
    task_handle,
)
from clio_agent.gact.server_surfaces import (
    ServerScope,
    ServerSurfaceRefusal,
    payload_messages,
    touched_surface_ids,
)
from clio_agent.tools.mcp_resource_updates import resource_updated

_SERVER = Path(__file__).resolve().parents[1] / "fixtures" / "plugins" / "host_semantics"
_spec = importlib.util.spec_from_file_location("hostfx_server", _SERVER / "server.py")
assert _spec is not None and _spec.loader is not None
hostfx = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(hostfx)

A2UI_MIME = "application/a2ui+json"
SCOPE = ServerScope(
    plugin="host-fixture",
    server="hostfx",
    namespace="hostfx",
    host={"catalogs": [basic_catalog_id()]},
)


# -- HS5: url trust ------------------------------------------------------------


@pytest.mark.parametrize(
    ("url", "kwargs", "expected"),
    [
        ("https://a.org/x", {}, ("elicitation_url_not_declared", "")),
        ("https://a.org/x", {"trusted": ["https://a.org"]}, (None, URL_TRUST_CONFIGURED)),
        ("https://p.org/x", {"plugin_origins": ["https://p.org"]}, (None, URL_TRUST_PLUGIN_ORIGIN)),
        (
            "https://evil.org/x",
            {"plugin_origins": ["https://p.org"]},
            ("elicitation_url_untrusted_origin", ""),
        ),
        (
            "http://127.0.0.1:4100/x",
            {"loopback": True, "launched": True},
            (None, URL_TRUST_PLUGIN_LOOPBACK),
        ),
        (
            "http://localhost:4100/x",
            {"loopback": True, "launched": True},
            (None, URL_TRUST_PLUGIN_LOOPBACK),
        ),
        (
            "http://127.0.0.1:4100/x",
            {"loopback": True, "launched": False},
            ("elicitation_url_insecure_scheme", ""),
        ),
        (
            "http://127.0.0.1/x",
            {"loopback": True, "launched": True},
            ("elicitation_url_insecure_scheme", ""),
        ),
        (
            "http://user@127.0.0.1:4100/x",
            {"loopback": True, "launched": True},
            ("elicitation_url_insecure_scheme", ""),
        ),
        (
            "http://p.org/x",
            {"plugin_origins": ["https://p.org"]},
            ("elicitation_url_insecure_scheme", ""),
        ),
    ],
)
def test_url_trust_names_where_an_accepted_origin_came_from(
    url: str, kwargs: dict[str, Any], expected: tuple[str | None, str]
) -> None:
    trusted = kwargs.pop("trusted", [])

    assert classify_url_trust(url, trusted, **kwargs) == expected


def test_url_metadata_names_the_origin_and_the_asking_server() -> None:
    block = build_url_metadata(
        "https://p.org/confirm?x=1",
        request_id="q1",
        namespace="hostfx",
        tool_name="open_link",
        invocation_id="inv",
        forwarded_from_session="",
        origin_trust=URL_TRUST_PLUGIN_ORIGIN,
        plugin="host-fixture",
        server="hostfx",
    )["elicitation"]

    assert block["origin"] == "https://p.org"
    assert block["origin_trust"] == URL_TRUST_PLUGIN_ORIGIN
    assert (block["plugin"], block["server"]) == ("host-fixture", "hostfx")
    assert block["url"] == "https://p.org/confirm?x=1"
    assert "plugin_title" not in block
    titled = build_url_metadata(
        "https://p.org/confirm",
        request_id="q1",
        namespace="hostfx",
        tool_name="open_link",
        invocation_id="inv",
        forwarded_from_session="",
        plugin="host-fixture",
        server="hostfx",
        plugin_title="Host fixture",
    )["elicitation"]
    assert titled["plugin_title"] == "Host fixture"


def test_a_refused_payload_names_the_plugin_title_only_when_it_has_one() -> None:
    from clio_agent.gact.server_surfaces import PayloadOutcome, publish_refusal

    events: list[Any] = []
    app = SimpleNamespace(
        state=SimpleNamespace(a2ui_catalogs=None, bus=SimpleNamespace(publish=events.append))
    )
    outcome = PayloadOutcome(uri="a2ui://x", reason="a2ui_payload_invalid", detail="bad")
    titled = ServerScope("host-fixture", "hostfx", "hostfx", {"title": "Host fixture"})

    publish_refusal(app, "s1", titled, tool="t", outcome=outcome)
    publish_refusal(app, "s1", SCOPE, tool="t", outcome=outcome)

    assert [event.type for event in events] == ["a2ui.server_surface.refused"] * 2
    assert events[0].payload["plugin_title"] == "Host fixture"
    assert "plugin_title" not in events[1].payload


# -- HS1: payloads ---------------------------------------------------------------


def test_server_scope_owns_its_surfaces_and_produces_its_catalogs() -> None:
    assert SCOPE.owner == {
        "kind": "server",
        "plugin": "host-fixture",
        "server": "hostfx",
        "namespace": "hostfx",
    }
    assert SCOPE.producible == frozenset({basic_catalog_id()})


def test_a_plugin_title_names_its_tabs_and_surfaces(monkeypatch: pytest.MonkeyPatch) -> None:
    rows = {
        "hostfx": {
            "plugin": "host-fixture",
            "host": {
                "title": "Host fixture",
                "tabs": [{"id": "panel", "title": "Panel", "resource": "a2ui://host-fixture/p"}],
            },
        },
        "plain": {
            "plugin": "plain",
            "host": {"tabs": [{"id": "t", "title": "T", "resource": "a2ui://plain/t"}]},
        },
    }
    monkeypatch.setattr(
        "clio_agent.gact.agents.resolution.session_resolution", lambda _app, _sid: None
    )
    monkeypatch.setattr(
        "clio_agent.gact.agents.prompt_basis.mounted_servers", lambda _app, _sid, _r: rows
    )

    titled, plain = [tabs_module._tab_wire(row) for row in tabs_module.declared_tabs(None, "s1")]

    assert titled["plugin_title"] == "Host fixture"
    assert "plugin_title" not in plain
    owner = ServerScope("host-fixture", "hostfx", "hostfx", rows["hostfx"]["host"]).owner
    assert owner["plugin_title"] == "Host fixture"
    assert "plugin_title" not in SCOPE.owner


@pytest.mark.parametrize(
    ("resource", "reason"),
    [
        ({"blob": "AAAA"}, "a2ui_payload_invalid"),
        ({"text": "not json"}, "a2ui_payload_invalid"),
        ({"text": "[]"}, "a2ui_payload_invalid"),
        ({"text": "[1]"}, "a2ui_payload_invalid"),
        ({"text": json.dumps({"version": "v0.8"})}, "a2ui_version_unsupported"),
    ],
)
def test_bad_payloads_are_refused_with_a_typed_reason(
    resource: dict[str, Any], reason: str
) -> None:
    with pytest.raises(ServerSurfaceRefusal) as caught:
        payload_messages(resource)

    assert caught.value.reason == reason


def test_a_payload_is_one_message_or_a_batch() -> None:
    batch = hostfx.panel_messages(2)

    assert payload_messages({"text": json.dumps(batch)}) == batch
    assert payload_messages({"text": json.dumps(batch[0])}) == [batch[0]]
    assert touched_surface_ids(batch) == [hostfx.SURFACE_ID]


# -- HS2 and HS4: actions ----------------------------------------------------------


def test_only_known_providers_are_sign_in_actions() -> None:
    assert login_provider("data_source/login/globus") == "globus"
    assert login_provider("data_source/login/nowhere") == ""
    assert login_provider("bump") == ""


def test_a_task_receipt_gives_its_handle() -> None:
    accepted = {"structuredContent": {"handle": "task_1", "accepted": True}}
    refused = {"structuredContent": {"handle": "task_1", "accepted": False}}

    assert task_handle(accepted) == "task_1"
    assert task_handle(refused) == ""
    assert task_handle({"structuredContent": {"handle": "other"}}) == ""
    assert task_handle({"content": []}) == ""


def test_only_app_visible_tools_take_surface_actions() -> None:
    tools = {
        "hostfx_bump": {"name": "hostfx_bump", "_meta": {"ui": {"visibility": ["app"]}}},
        "hostfx_hidden": {"name": "hostfx_hidden", "_meta": {"ui": {"visibility": ["model"]}}},
        "hostfx_plain": {"name": "hostfx_plain"},
    }
    executor = SimpleNamespace(get_all_tool_definitions=lambda: tools)

    assert resolve_server_tool(executor, "hostfx", "bump") == "hostfx_bump"
    assert resolve_server_tool(executor, "hostfx", "plain") == "hostfx_plain"
    for name in ("hidden", "missing", ""):
        with pytest.raises(ServerActionError) as caught:
            resolve_server_tool(executor, "hostfx", name)
        assert (caught.value.status, caught.value.reason) == (
            403,
            "a2ui_server_tool_not_app_visible",
        )


def test_surface_actions_resolve_from_the_servers_own_tool_list() -> None:
    listed = [
        SimpleNamespace(name="bump", meta={"ui": {"visibility": ["app"]}}),
        SimpleNamespace(name="hidden", meta={"ui": {"visibility": ["model"]}}),
    ]
    executor = SimpleNamespace(
        get_all_tool_definitions=lambda: {},  # no turn has run yet
        list_namespace_tools=lambda namespace: listed if namespace == "hostfx" else [],
    )

    assert resolve_server_tool(executor, "hostfx", "bump") == "hostfx_bump"
    for namespace, name in (("hostfx", "hidden"), ("hostfx", "gone"), ("other", "bump")):
        with pytest.raises(ServerActionError) as caught:
            resolve_server_tool(executor, namespace, name)
        assert caught.value.reason == "a2ui_server_tool_not_app_visible"


def test_an_outcome_reports_what_the_action_did() -> None:
    applied = ServerActionOutcome(tool="bump", outcome="applied", surface_ids=["s"])
    pending = ServerActionOutcome(tool="slow", outcome="task_pending", task_handle="task_1")

    assert applied.wire("act_1") == {
        "status": "delivered",
        "destination": "server",
        "action_id": "act_1",
        "tool": "bump",
        "outcome": "applied",
        "surface_ids": ["s"],
    }
    assert pending.wire("act_2")["task_handle"] == "task_1"


# -- HS3: tabs -------------------------------------------------------------------


class _Bus:
    def __init__(self) -> None:
        self.events: list[Any] = []

    def publish(self, event: Any) -> None:
        self.events.append(event)

    def of(self, kind: str) -> list[dict[str, Any]]:
        return [event.payload for event in self.events if event.type == kind]


def _a2ui(messages: list[dict[str, Any]]) -> dict[str, Any]:
    return {"uri": hostfx.PANEL_URI, "mimeType": A2UI_MIME, "text": json.dumps(messages)}


class _Executor:
    """The fixture server's behavior, without a connection."""

    def __init__(self) -> None:
        self.count = 0
        self.watching: dict[tuple[str, str], bool] = {}
        self.calls: list[tuple[str, dict[str, Any]]] = []
        self.fail_read: Exception | None = None
        self.subscribes: list[tuple[str, Any]] = []
        self._namespace_servers: dict[str, Any] = {"hostfx": object()}

    def read_resource(self, namespace: str, uri: str) -> Any:
        if self.fail_read is not None:
            raise self.fail_read
        assert (namespace, uri) == ("hostfx", hostfx.PANEL_URI)
        return {"contents": [_a2ui(hostfx.panel_messages(self.count))]}

    def set_resource_subscription(self, namespace: str, uri: str, *, subscribed: bool) -> bool:
        self.watching[(namespace, uri)] = subscribed
        if subscribed:
            self.subscribes.append((namespace, self._namespace_servers[namespace]))
        return subscribed

    def get_all_tool_definitions(self) -> dict[str, Any]:
        return {}  # the turn catalog: empty until a turn runs, as after an agent restart

    def list_namespace_tools(self, namespace: str) -> list[Any]:
        assert namespace == "hostfx"
        return [{"name": "bump", "_meta": {"ui": {"visibility": ["app"]}}}]

    def call_tool_result(self, name: str, arguments: dict[str, Any]) -> Any:
        self.calls.append((name, arguments))
        self.count += int(arguments.get("by", 1))
        return {
            "content": [
                {"type": "text", "text": f"Count is now {self.count}."},
                {
                    "type": "resource",
                    "resource": _a2ui(hostfx.panel_messages(self.count, create=False)),
                },
            ]
        }


@pytest.fixture
def tab_app(monkeypatch: pytest.MonkeyPatch) -> tuple[Any, _Executor]:
    executor = _Executor()
    registry = CatalogRegistry()
    session = SimpleNamespace(workspace_id="ws_1")
    app = SimpleNamespace(
        state=SimpleNamespace(
            bus=_Bus(),
            sessions=SimpleNamespace(get=lambda sid: session if sid == "s1" else None),
            connected_storage=SimpleNamespace(store=SimpleNamespace(clio_id="clio_1")),
        )
    )
    declared = {
        "id": "host-fixture/panel",
        "plugin": "host-fixture",
        "tab": "panel",
        "title": "Fixture panel",
        "icon": "layout",
        "server": "hostfx",
        "resource": hostfx.PANEL_URI,
    }

    async def run_bound(_app: Any, _sid: str, operation: Any, *, request: Any = None) -> Any:
        return operation(executor)

    monkeypatch.setattr(
        tabs_module, "declared_tabs", lambda _app, sid: [dict(declared)] if sid == "s1" else []
    )
    monkeypatch.setattr(
        tabs_module, "server_scope", lambda _app, sid, ns: SCOPE if ns == "hostfx" else None
    )
    monkeypatch.setattr("clio_agent.gact.mcp_apps._run_bound", run_bound)
    monkeypatch.setattr(
        "clio_agent.gact.a2ui_catalogs.activation.session_catalog_resolver",
        lambda _app, _sid: registry,
    )
    return app, executor


def _action(name: str, context: dict[str, Any], stamp: str = "2026-07-28T00:00:00Z") -> Any:
    return {
        "message": {
            "version": "v0.9.1",
            "action": {
                "name": name,
                "surfaceId": hostfx.SURFACE_ID,
                "sourceComponentId": "bump",
                "timestamp": stamp,
                "context": context,
            },
        }
    }


async def test_a_tab_lists_opens_watches_and_closes(tab_app: tuple[Any, _Executor]) -> None:
    app, executor = tab_app
    tabs = tabs_module.PluginTabs(app)
    try:
        listed = tabs.list("s1")
        assert [(row["id"], row["available"]) for row in listed] == [("host-fixture/panel", True)]
        assert tabs.list("other") == []

        opened = await tabs.open("s1", "host-fixture", "panel")

        assert opened["revision"] == 1
        assert [surface["id"] for surface in opened["surfaces"]] == [hostfx.SURFACE_ID]
        assert opened["surfaces"][0]["owner"] == SCOPE.owner
        assert executor.watching == {("hostfx", hostfx.PANEL_URI): True}

        again = await tabs.open("s1", "host-fixture", "panel")
        assert again["revision"] == 2

        await tabs.close("s1", "host-fixture", "panel")
        assert executor.watching == {("hostfx", hostfx.PANEL_URI): False}
    finally:
        tabs.shutdown()


async def test_a_tab_not_in_the_session_is_not_found(tab_app: tuple[Any, _Executor]) -> None:
    app, _executor = tab_app
    tabs = tabs_module.PluginTabs(app)
    try:
        with pytest.raises(tabs_module.PluginTabError) as caught:
            await tabs.open("other", "host-fixture", "panel")
        assert (caught.value.status, caught.value.reason) == (404, "plugin_tab_not_found")

        with pytest.raises(tabs_module.PluginTabError) as not_open:
            await tabs.action("s1", "host-fixture", "panel", _action("bump", {"by": 1}))
        assert not_open.value.reason == "plugin_tab_not_open"
    finally:
        tabs.shutdown()


async def test_an_update_re_reads_the_tab_and_publishes_its_changes(
    tab_app: tuple[Any, _Executor],
) -> None:
    app, executor = tab_app
    bus: _Bus = app.state.bus
    tabs = tabs_module.PluginTabs(app)
    try:
        await tabs.open("s1", "host-fixture", "panel")
        executor.count = 5

        resource_updated("hostfx", hostfx.PANEL_URI)
        for _ in range(200):
            if bus.of(tabs_module.REFRESHED):
                break
            await asyncio.sleep(0.01)

        upserted = bus.of(tabs_module.SURFACE_UPSERTED)
        refreshed = bus.of(tabs_module.REFRESHED)
        assert [row["revision"] for row in upserted] == [2]
        assert "Count: 5" in json.dumps(upserted[0]["surface"])
        assert refreshed == [
            {"tab_id": "host-fixture/panel", "revision": 2, "surface_ids": [hostfx.SURFACE_ID]}
        ]

        resource_updated("other", hostfx.PANEL_URI)
        await asyncio.sleep(0.05)
        assert len(bus.of(tabs_module.REFRESHED)) == 1
    finally:
        tabs.shutdown()


async def _until(predicate: Any) -> None:
    for _ in range(200):
        if predicate():
            return
        await asyncio.sleep(0.01)


async def test_a_surface_revision_keeps_rising_across_re_reads(
    tab_app: tuple[Any, _Executor],
) -> None:
    app, executor = tab_app
    bus: _Bus = app.state.bus
    tabs = tabs_module.PluginTabs(app)
    try:
        opened = await tabs.open("s1", "host-fixture", "panel")
        first = opened["surfaces"][0]["revision"]

        async def re_read(count: int) -> dict[str, Any]:
            executor.count = count
            seen = len(bus.of(tabs_module.REFRESHED))
            resource_updated("hostfx", hostfx.PANEL_URI)
            await _until(lambda: len(bus.of(tabs_module.REFRESHED)) > seen)
            return bus.of(tabs_module.SURFACE_UPSERTED)[-1]["surface"]

        changed = await re_read(5)
        unchanged = await re_read(5)
        changed_again = await re_read(7)

        # A fresh fold would give the same message count each time; the host carries on.
        assert changed["revision"] == first + 1
        assert unchanged["revision"] == first + 1
        assert unchanged["message_revisions"] == changed["message_revisions"]
        assert changed_again["revision"] == first + 2
        assert max(changed_again["message_revisions"]) == first + 2
        assert min(changed_again["message_revisions"]) < first + 2  # createSurface kept

        reopened = await tabs.open("s1", "host-fixture", "panel")
        assert reopened["surfaces"][0]["revision"] == first + 2
    finally:
        tabs.shutdown()


@pytest.mark.parametrize("reason", ["configuration_switched", "grant_restart"])
async def test_a_remounted_server_is_watched_again_and_re_read(
    tab_app: tuple[Any, _Executor], reason: str
) -> None:
    from clio_agent.tools.fleet_close_listeners import proxies_dropped

    app, executor = tab_app
    bus: _Bus = app.state.bus
    tabs = tabs_module.PluginTabs(app)
    try:
        await tabs.open("s1", "host-fixture", "panel")
        old = executor._namespace_servers["hostfx"]
        assert executor.subscribes == [("hostfx", old)]

        # A plugin update or rebind mounts a new server process for the namespace, at
        # once or (``grant_restart``) when the reaper drains a deferred restart.
        executor._namespace_servers["hostfx"] = new = object()
        executor.count = 3
        proxies_dropped("root", (object(),), reason)  # someone else's
        proxies_dropped("root", (old,), reason)
        await _until(lambda: bus.of(tabs_module.REFRESHED))

        assert executor.subscribes == [("hostfx", old), ("hostfx", new)]
        assert "Count: 3" in json.dumps(bus.of(tabs_module.SURFACE_UPSERTED)[-1]["surface"])

        # An idle reap only forgets the watch; the tab's next open starts it again.
        proxies_dropped("root", (new,), "idle_ttl")
        await asyncio.sleep(0.05)
        assert len(executor.subscribes) == 2
        await tabs.open("s1", "host-fixture", "panel")
        assert executor.subscribes[-1] == ("hostfx", new)
        assert len(executor.subscribes) == 3
    finally:
        tabs.shutdown()


async def test_an_agent_change_watches_the_open_tabs_again(
    tab_app: tuple[Any, _Executor],
) -> None:
    app, executor = tab_app
    bus: _Bus = app.state.bus
    tabs = tabs_module.PluginTabs(app)
    try:
        tabs_module.session_agent_changed(app, "s1")  # nothing open yet: no-op
        app.state.plugin_tabs = tabs
        await tabs.open("s1", "host-fixture", "panel")
        executor._namespace_servers["hostfx"] = readded = object()

        tabs_module.session_agent_changed(app, "other")
        tabs_module.session_agent_changed(app, "s1")
        await _until(lambda: bus.of(tabs_module.REFRESHED))

        assert executor.subscribes[-1] == ("hostfx", readded)
        assert len(executor.subscribes) == 2
        assert len(bus.of(tabs_module.REFRESHED)) == 1
    finally:
        tabs.shutdown()


async def test_a_failed_read_marks_the_tab_unavailable(tab_app: tuple[Any, _Executor]) -> None:
    app, executor = tab_app
    bus: _Bus = app.state.bus
    tabs = tabs_module.PluginTabs(app)
    try:
        executor.fail_read = ValueError("unknown MCP namespace 'hostfx'")
        opened = await tabs.open("s1", "host-fixture", "panel")

        assert opened["surfaces"] == []
        assert opened["tab"]["reason"] == tabs_module.SERVER_UNAVAILABLE
        assert opened["tab"]["available"] is False
        assert bus.of(tabs_module.UNAVAILABLE)[0]["reason"] == tabs_module.SERVER_UNAVAILABLE
        assert tabs.list("s1")[0]["available"] is False

        executor.fail_read = None
        reopened = await tabs.open("s1", "host-fixture", "panel")
        assert reopened["tab"]["available"] is True
        assert tabs.list("s1")[0]["available"] is True
    finally:
        tabs.shutdown()


async def test_a_tab_action_calls_its_server_and_applies_the_result(
    tab_app: tuple[Any, _Executor],
) -> None:
    app, executor = tab_app
    bus: _Bus = app.state.bus
    tabs = tabs_module.PluginTabs(app)
    try:
        await tabs.open("s1", "host-fixture", "panel")

        result = await tabs.action("s1", "host-fixture", "panel", _action("bump", {"by": 3}))

        assert executor.calls == [("hostfx_bump", {"by": 3})]
        assert result["status"] == "delivered"
        assert result["destination"] == "server"
        assert result["outcome"] == "applied"
        assert result["surface_ids"] == [hostfx.SURFACE_ID]
        upserted = bus.of(tabs_module.SURFACE_UPSERTED)
        assert [row["revision"] for row in upserted] == [2]
        assert "Count: 3" in json.dumps(upserted[0]["surface"])

        repeat = await tabs.action("s1", "host-fixture", "panel", _action("bump", {"by": 3}))
        assert repeat == result
        assert len(executor.calls) == 1
    finally:
        tabs.shutdown()


async def test_a_tab_sign_in_action_stays_with_the_client(tab_app: tuple[Any, _Executor]) -> None:
    app, executor = tab_app
    tabs = tabs_module.PluginTabs(app)
    try:
        await tabs.open("s1", "host-fixture", "panel")

        result = await tabs.action(
            "s1", "host-fixture", "panel", _action("data_source/login/globus", {})
        )

        assert executor.calls == []
        assert {key: result[key] for key in result if key != "action_id"} == {
            "name": "data_source/login/globus",
            "status": "accepted",
            "state": "consumed",
            "delivery": "client_ui",
            "destination": "client",
            "provider": "globus",
            "context": {"clio_id": "clio_1", "workspace_id": "ws_1"},
        }
    finally:
        tabs.shutdown()


async def test_a_tab_action_body_is_the_action_wrapper(tab_app: tuple[Any, _Executor]) -> None:
    app, _executor = tab_app
    tabs = tabs_module.PluginTabs(app)
    try:
        await tabs.open("s1", "host-fixture", "panel")
        bare = _action("bump", {})["message"]

        with pytest.raises(tabs_module.PluginTabError) as caught:
            await tabs.action("s1", "host-fixture", "panel", bare)
        assert (caught.value.status, caught.value.reason) == (422, "validation_error")
    finally:
        tabs.shutdown()


async def test_a_refused_tool_is_a_typed_refusal(tab_app: tuple[Any, _Executor]) -> None:
    app, _executor = tab_app
    tabs = tabs_module.PluginTabs(app)
    try:
        await tabs.open("s1", "host-fixture", "panel")

        with pytest.raises(tabs_module.PluginTabError) as caught:
            await tabs.action("s1", "host-fixture", "panel", _action("missing", {}))
        assert (caught.value.status, caught.value.reason) == (
            403,
            "a2ui_server_tool_not_app_visible",
        )
    finally:
        tabs.shutdown()


def test_server_actions_module_names_no_particular_server() -> None:
    source = Path(server_actions.__file__).read_text(encoding="utf-8").lower()

    assert "relay" not in source
    assert "globus" not in source
