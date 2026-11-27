"""A plugin's MCP server environments are built at install, and that never fails it."""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest

from clio_agent.gact import plugin_prebuild
from clio_agent.tools import mcp_prepare
from clio_agent.tools.mcp_config import MCPServerSpec


def _record() -> Any:
    return SimpleNamespace(id="demo", mcp_servers={"demo": {"command": "uvx"}})


def _spec(name: str) -> MCPServerSpec:
    return MCPServerSpec(name=name, transport="stdio", command="uvx", env={"A": "1"})


def test_every_stdio_server_is_provisioned_with_bytecode_compiled(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    seen: dict[str, dict[str, str]] = {}

    def provision(namespace: str, spec: Any, **_: Any) -> str:
        seen[namespace] = dict(spec.env)
        return mcp_prepare.PROVISIONED

    monkeypatch.setattr(plugin_prebuild, "_plugin_specs", lambda app, record: {"s": _spec("s")})
    monkeypatch.setattr(mcp_prepare, "provision", provision)

    assert plugin_prebuild.prebuild(object(), _record()) == {"s": mcp_prepare.PROVISIONED}
    assert seen == {"s": {"UV_COMPILE_BYTECODE": "1", "A": "1"}}


def test_a_failed_build_is_reported_not_raised(monkeypatch: pytest.MonkeyPatch) -> None:
    def provision(namespace: str, spec: Any, **_: Any) -> str:
        if namespace == "bad":
            raise mcp_prepare.PrepareError(mcp_prepare.LAUNCHER_MISSING, "uvx is not on PATH")
        return mcp_prepare.PROVISION_CACHED

    specs = {"bad": _spec("bad"), "good": _spec("good")}
    monkeypatch.setattr(plugin_prebuild, "_plugin_specs", lambda app, record: specs)
    monkeypatch.setattr(mcp_prepare, "provision", provision)

    outcomes = plugin_prebuild.prebuild(object(), _record())

    assert outcomes["good"] == mcp_prepare.PROVISION_CACHED
    assert outcomes["bad"].startswith("failed:")


def test_unreadable_declarations_skip_the_build(monkeypatch: pytest.MonkeyPatch) -> None:
    def broken(app: Any, record: Any) -> dict[str, Any]:
        raise ValueError("bad mcp.json")

    monkeypatch.setattr(plugin_prebuild, "_plugin_specs", broken)

    assert plugin_prebuild.prebuild(object(), _record()) == {"*": "skipped: bad mcp.json"}


def test_schedule_builds_each_trusted_page_plugin_once(monkeypatch: pytest.MonkeyPatch) -> None:
    page = {"navigation": {"id": "p"}}
    trusted = SimpleNamespace(marketplace="m", id="a", root="/a", mcp_servers={"a": {}}, host=page)
    untrusted = SimpleNamespace(
        marketplace="m", id="b", root="/b", mcp_servers={"b": {}}, host=page
    )
    no_servers = SimpleNamespace(marketplace="m", id="c", root="/c", mcp_servers={}, host=page)
    no_page = SimpleNamespace(marketplace="m", id="d", root="/d", mcp_servers={"d": {}}, host={})
    store = SimpleNamespace(
        installed_records=lambda: [trusted, untrusted, no_servers, no_page],
        trusted=lambda record: record is not untrusted,
    )
    built: list[str] = []
    prestarted: list[str] = []
    monkeypatch.setattr(plugin_prebuild, "enabled", lambda: True)
    monkeypatch.setattr(
        "clio_agent.gact.agents.resolution.plugin_service", lambda app: SimpleNamespace(store=store)
    )
    monkeypatch.setattr(
        plugin_prebuild, "prebuild", lambda app, record: built.append(record.id) or {}
    )
    monkeypatch.setattr(
        plugin_prebuild, "prestart", lambda app, record: prestarted.append(record.id)
    )
    plugin_prebuild.forget()
    try:
        for _ in range(2):
            thread = plugin_prebuild.schedule(object())
            assert thread is not None
            thread.join(timeout=10)
    finally:
        plugin_prebuild.forget()

    assert built == ["a"]
    assert prestarted == ["a"]


def test_a_failed_build_is_not_prestarted(monkeypatch: pytest.MonkeyPatch) -> None:
    page = {"navigation": {"id": "p"}}
    record = SimpleNamespace(marketplace="m", id="a", root="/a", mcp_servers={"a": {}}, host=page)
    store = SimpleNamespace(installed_records=lambda: [record], trusted=lambda record: True)
    prestarted: list[str] = []
    monkeypatch.setattr(plugin_prebuild, "enabled", lambda: True)
    monkeypatch.setattr(
        "clio_agent.gact.agents.resolution.plugin_service", lambda app: SimpleNamespace(store=store)
    )
    monkeypatch.setattr(plugin_prebuild, "prebuild", lambda app, record: {"a": "failed: no uv"})
    monkeypatch.setattr(
        plugin_prebuild, "prestart", lambda app, record: prestarted.append(record.id)
    )
    plugin_prebuild.forget()
    try:
        thread = plugin_prebuild.schedule(object())
        assert thread is not None
        thread.join(timeout=10)
    finally:
        plugin_prebuild.forget()

    assert prestarted == []


def test_a_page_is_prestarted_in_its_own_context_and_held(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import asyncio
    import threading

    from clio_agent.gact import plugin_pages, session_warmup

    loop = asyncio.new_event_loop()
    runner = threading.Thread(target=loop.run_forever, daemon=True)
    runner.start()
    app = SimpleNamespace(state=SimpleNamespace(mcp_app_loop=loop))
    opened: list[tuple[str, str, bool]] = []
    warmed: list[tuple[str, str]] = []
    held: list[tuple[str, float | None]] = []

    class Pages:
        async def context(self, plugin: str, page: str, *, create: bool) -> str:
            opened.append((plugin, page, create))
            return "ctx_1"

    monkeypatch.setattr(plugin_prebuild, "prestart_s", lambda: 60.0)
    monkeypatch.setattr(plugin_pages, "plugin_pages", lambda app: Pages())
    monkeypatch.setattr(
        session_warmup,
        "start_session_warmup",
        lambda app, sid, *, trigger: warmed.append((sid, trigger)),
    )
    monkeypatch.setattr(
        session_warmup,
        "hold_session_fleet",
        lambda app, sid, *, seconds=None: held.append((sid, seconds)),
    )
    record = SimpleNamespace(id="demo", host={"navigation": {"id": "p"}})
    try:
        assert plugin_prebuild.prestart(app, record) == "ctx_1"
    finally:
        loop.call_soon_threadsafe(loop.stop)
        runner.join(timeout=10)
        loop.close()

    assert opened == [("demo", "p", True)]
    assert warmed == [("ctx_1", "page_prestart")]
    assert held == [("ctx_1", 60.0)]


def test_prestarting_off_or_without_a_loop_does_nothing(monkeypatch: pytest.MonkeyPatch) -> None:
    record = SimpleNamespace(id="demo", host={"navigation": {"id": "p"}})
    monkeypatch.setattr(plugin_prebuild, "prestart_s", lambda: 0.0)
    assert plugin_prebuild.prestart(SimpleNamespace(state=SimpleNamespace()), record) is None
    monkeypatch.setattr(plugin_prebuild, "prestart_s", lambda: 60.0)
    assert plugin_prebuild.prestart(object(), record) is None


def test_the_first_page_listing_schedules_once(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[object] = []
    monkeypatch.setattr(plugin_prebuild, "schedule", lambda app: calls.append(app))
    plugin_prebuild.forget()
    try:
        plugin_prebuild.schedule_once("app")
        plugin_prebuild.schedule_once("app")
    finally:
        plugin_prebuild.forget()

    assert calls == ["app"]


def test_schedule_does_nothing_when_turned_off(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(plugin_prebuild, "enabled", lambda: False)

    assert plugin_prebuild.schedule(object()) is None
