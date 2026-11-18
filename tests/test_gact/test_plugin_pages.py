"""Plugin pages: a plugin's page in the host's primary navigation (HS3)."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from clio_agent.gact import plugin_pages as pages_module
from clio_agent.gact import plugin_tabs as tabs_module
from clio_agent.gact.a2ui_catalogs.registry import CatalogRegistry
from tests.test_gact.test_host_semantics import SCOPE, _action, _Bus, _Executor, hostfx

PAGE = {
    "id": "page",
    "title": "Fixture page",
    "icon": "layout",
    "server": "hostfx",
    "resource": hostfx.PANEL_URI,
}


class _Sessions:
    def __init__(self) -> None:
        self.rows: dict[str, Any] = {
            "s1": SimpleNamespace(id="s1", workspace_id="ws_1", metadata={}),
        }
        self.created = 0

    def get(self, sid: str) -> Any:
        return self.rows.get(sid)

    def select(self, query: Any) -> list[Any]:
        assert query.kinds == frozenset({"page"})
        return [row for row in self.rows.values() if pages_module.page_of(row)]

    def add_context(self, plugin: str, page: str) -> str:
        self.created += 1
        sid = f"ctx{self.created}"
        self.rows[sid] = SimpleNamespace(
            id=sid,
            workspace_id=pages_module.PAGE_WORKSPACE,
            metadata={pages_module.PAGE_METADATA_KEY: {"plugin": plugin, "page": page}},
        )
        return sid


class _Store:
    def __init__(self, records: list[Any], trusted: set[str]) -> None:
        self._records = records
        self._trusted = trusted

    def records(self) -> list[Any]:
        return list(self._records)

    def trusted(self, record: Any) -> bool:
        return record.id in self._trusted


@pytest.fixture
def page_app(monkeypatch: pytest.MonkeyPatch) -> tuple[Any, _Executor, _Sessions]:
    executor = _Executor()
    registry = CatalogRegistry()
    sessions = _Sessions()
    records = [
        SimpleNamespace(id="plain", host={"tabs": []}),
        SimpleNamespace(id="host-fixture", host={"title": "Host fixture", "navigation": PAGE}),
        SimpleNamespace(
            id="stranger", host={"navigation": {**PAGE, "resource": "a2ui://stranger/page"}}
        ),
    ]
    store = _Store(records, trusted={"plain", "host-fixture"})
    app = SimpleNamespace(
        state=SimpleNamespace(
            bus=_Bus(),
            sessions=sessions,
            connected_storage=SimpleNamespace(store=SimpleNamespace(clio_id="clio_1")),
            plugin_service=SimpleNamespace(ready=lambda: None, store=store),
            session_defaults=SimpleNamespace(
                get=lambda: SimpleNamespace(provider_id="", model_id="")
            ),
        )
    )
    panel = {
        "id": "host-fixture/panel",
        "plugin": "host-fixture",
        "tab": "panel",
        "title": "Fixture panel",
        "icon": "layout",
        "server": "hostfx",
        "resource": hostfx.PANEL_URI,
        "placement": "tab",
    }
    page = {**panel, "id": "host-fixture/page", "tab": "page", "placement": "primary"}

    async def run_bound(_app: Any, _sid: str, operation: Any, *, request: Any = None) -> Any:
        return operation(executor)

    monkeypatch.setattr(tabs_module, "declared_tabs", lambda _app, sid: [dict(panel), dict(page)])
    monkeypatch.setattr(
        tabs_module, "server_scope", lambda _app, sid, ns: SCOPE if ns == "hostfx" else None
    )
    monkeypatch.setattr("clio_agent.gact.mcp_apps._run_bound", run_bound)
    monkeypatch.setattr(
        "clio_agent.gact.a2ui_catalogs.activation.session_catalog_resolver",
        lambda _app, _sid: registry,
    )
    monkeypatch.setattr(
        pages_module,
        "_create_context",
        lambda _app, row: sessions.add_context(row["plugin"], row["page"]),
    )
    monkeypatch.setattr("clio_agent.gact.session_warmup.start_session_warmup", lambda *a, **k: None)
    return app, executor, sessions


def _shutdown(app: Any) -> None:
    tabs = getattr(app.state, "plugin_tabs", None)
    if tabs is not None:
        tabs.shutdown()


def test_a_context_without_a_model_adopts_the_default_chosen_since() -> None:
    rows = {
        "bare": SimpleNamespace(id="bare", model=None),
        "set": SimpleNamespace(id="set", model={"provider_id": "p0", "model_id": "m0"}),
    }
    updates: list[tuple[str, Any]] = []
    defaults = SimpleNamespace(provider_id="", model_id="")
    app = SimpleNamespace(
        state=SimpleNamespace(
            sessions=SimpleNamespace(
                get=rows.get, update=lambda sid, *, model: updates.append((sid, model))
            ),
            session_defaults=SimpleNamespace(get=lambda: defaults),
        )
    )

    pages_module._adopt_default_model(app, "bare")
    assert updates == []

    defaults.provider_id, defaults.model_id = "p1", "m1"
    pages_module._adopt_default_model(app, "bare")
    pages_module._adopt_default_model(app, "set")
    assert updates == [("bare", {"provider_id": "p1", "model_id": "m1"})]


async def test_pages_list_every_declared_page_with_its_trust(
    page_app: tuple[Any, _Executor, _Sessions],
) -> None:
    app, _executor, _sessions = page_app
    try:
        listed = await pages_module.plugin_pages(app).list()
    finally:
        _shutdown(app)

    assert listed == [
        {
            "id": "host-fixture/page",
            "plugin": "host-fixture",
            "page": "page",
            "title": "Fixture page",
            "icon": "layout",
            "server": "hostfx",
            "resource": hostfx.PANEL_URI,
            "placement": "primary",
            "available": True,
            "reason": "",
            "plugin_title": "Host fixture",
            "context_session_id": None,
        },
        {
            "id": "stranger/page",
            "plugin": "stranger",
            "page": "page",
            "title": "Fixture page",
            "icon": "layout",
            "server": "hostfx",
            "resource": "a2ui://stranger/page",
            "placement": "primary",
            "available": False,
            "reason": pages_module.PLUGIN_NOT_TRUSTED,
            "context_session_id": None,
        },
    ]


async def test_a_page_opens_in_one_host_owned_context(
    page_app: tuple[Any, _Executor, _Sessions],
) -> None:
    app, executor, sessions = page_app
    pages = pages_module.plugin_pages(app)
    try:
        opened = await pages.open("host-fixture", "page")
        again = await pages.open("host-fixture", "page")
        listed = await pages.list()
    finally:
        _shutdown(app)

    assert sessions.created == 1
    assert opened["context_session_id"] == again["context_session_id"] == "ctx1"
    assert opened["page"]["id"] == "host-fixture/page"
    assert opened["page"]["page"] == "page"
    assert opened["page"]["placement"] == "primary"
    assert [surface["id"] for surface in opened["surfaces"]] == [hostfx.SURFACE_ID]
    assert (opened["revision"], again["revision"]) == (1, 2)
    assert executor.watching == {("hostfx", hostfx.PANEL_URI): True}
    assert listed[0]["context_session_id"] == "ctx1"


async def test_concurrent_first_opens_share_one_context(
    page_app: tuple[Any, _Executor, _Sessions],
) -> None:
    app, _executor, sessions = page_app
    pages = pages_module.plugin_pages(app)
    try:
        first, second = await asyncio.gather(
            pages.open("host-fixture", "page"), pages.open("host-fixture", "page")
        )
    finally:
        _shutdown(app)

    assert sessions.created == 1
    assert first["context_session_id"] == second["context_session_id"]


async def test_a_page_action_applies_and_publishes_page_events(
    page_app: tuple[Any, _Executor, _Sessions],
) -> None:
    app, executor, _sessions = page_app
    bus: _Bus = app.state.bus
    pages = pages_module.plugin_pages(app)
    try:
        await pages.open("host-fixture", "page")
        result = await pages.action("host-fixture", "page", _action("bump", {"by": 2}))
    finally:
        _shutdown(app)

    assert executor.calls == [("hostfx_bump", {"by": 2})]
    assert result["outcome"] == "applied"
    assert bus.of(tabs_module.SURFACE_UPSERTED) == []
    upserted = bus.of("plugin_page.surface.upserted")
    assert [(row["page_id"], row["revision"]) for row in upserted] == [("host-fixture/page", 2)]
    assert "tab_id" not in upserted[0]
    assert "Count: 2" in json.dumps(upserted[0]["surface"])
    assert {event.session_id for event in bus.events} == {"ctx1"}


async def test_page_refusals_are_typed(page_app: tuple[Any, _Executor, _Sessions]) -> None:
    app, _executor, sessions = page_app
    pages = pages_module.plugin_pages(app)
    try:
        with pytest.raises(tabs_module.PluginTabError) as not_open:
            await pages.action("host-fixture", "page", _action("bump", {"by": 1}))
        with pytest.raises(tabs_module.PluginTabError) as missing:
            await pages.open("host-fixture", "other")
        with pytest.raises(tabs_module.PluginTabError) as untrusted:
            await pages.open("stranger", "page")
        closed = await pages.close("host-fixture", "page")
    finally:
        _shutdown(app)

    assert (not_open.value.status, not_open.value.reason) == (404, "plugin_page_not_open")
    assert (missing.value.status, missing.value.reason) == (404, "plugin_page_not_found")
    assert (untrusted.value.status, untrusted.value.reason) == (
        409,
        pages_module.PLUGIN_NOT_TRUSTED,
    )
    assert closed == {"closed": True}
    assert sessions.created == 0


async def test_a_page_is_no_tab_of_an_ordinary_session(
    page_app: tuple[Any, _Executor, _Sessions],
) -> None:
    app, _executor, _sessions = page_app
    tabs = tabs_module.plugin_tabs(app)
    try:
        listed = tabs.list("s1")
        with pytest.raises(tabs_module.PluginTabError) as caught:
            await tabs.open("s1", "host-fixture", "page")
        opened = await tabs.open("s1", "host-fixture", "panel")
    finally:
        _shutdown(app)

    assert [(row["id"], row["placement"]) for row in listed] == [("host-fixture/panel", "tab")]
    assert caught.value.reason == "plugin_tab_not_found"
    assert opened["tab"]["placement"] == "tab"


def test_only_a_page_contexts_own_tab_events_are_renamed() -> None:
    context = SimpleNamespace(
        id="ctx",
        metadata={pages_module.PAGE_METADATA_KEY: {"plugin": "p", "page": "home"}},
    )
    rows = {"ctx": context, "s1": SimpleNamespace(id="s1", metadata={})}
    app = SimpleNamespace(state=SimpleNamespace(sessions=SimpleNamespace(get=rows.get)))
    payload = {"tab_id": "p/home", "revision": 3}

    assert pages_module.page_event(app, "ctx", "plugin_tab.refreshed", payload) == (
        "plugin_page.refreshed",
        {"page_id": "p/home", "revision": 3},
    )
    assert pages_module.page_event(app, "s1", "plugin_tab.refreshed", payload) == (
        "plugin_tab.refreshed",
        payload,
    )
    other = {"tab_id": "p/panel", "revision": 1}
    assert pages_module.page_event(app, "ctx", "plugin_tab.refreshed", other)[0] == (
        "plugin_tab.refreshed"
    )
    assert pages_module.page_event(app, "ctx", "session.updated", payload)[0] == "session.updated"


def test_plugin_pages_module_names_no_particular_plugin() -> None:
    source = Path(pages_module.__file__).read_text(encoding="utf-8")
    assert "relay" not in source.lower()
