"""Applying a connector's choices again keeps its running servers when nothing changed.

Every gateway refresh moves the MCP configuration revision, and each workspace fleet built
under the old revision is rebuilt at its next idle point, so every conversation's servers
restart and lose their state (an open scene, a notebook kernel). Enabling an enabled
connector again, or answering its setup card a second time, must not cost that.
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from clio_agent.gact import connectors_service as service


def _app() -> Any:
    from tests.test_connectors.conftest import FakeSessions

    return SimpleNamespace(state=SimpleNamespace(bus=None, sessions=FakeSessions(), agent=object()))


@pytest.fixture
def applied(fixture_catalog: Path, monkeypatch: pytest.MonkeyPatch) -> dict[str, list[Any]]:
    from clio_agent.gact import connector_follow, connector_rebind
    from clio_agent.gact.routes import mcp_configuration

    calls: dict[str, list[Any]] = {"refresh": [], "probe": [], "rebind": []}

    async def refresh(_app: Any) -> None:
        calls["refresh"].append(True)

    async def probe(app: Any, found: Any, choice: Any, *, force: bool = False) -> dict:
        calls["probe"].append(force)
        cached = service._probes(app).get(found.id)
        if cached and cached["key"] == service._probe_key(found, choice) and not force:
            return cached
        result = {
            "key": service._probe_key(found, choice),
            "tool_count": 1,
            "status": "ready",
            "reason": None,
        }
        service._probes(app)[found.id] = result
        return result

    def rebind(_agent: Any, namespace: str, _saved: Any, _failed: dict) -> list[str]:
        calls["rebind"].append(namespace)
        return []

    monkeypatch.setattr(mcp_configuration, "_refresh_agent_tool_gateway", refresh)
    monkeypatch.setattr(service, "probe", probe)
    monkeypatch.setattr(connector_rebind, "rebind_namespace", rebind)
    monkeypatch.setattr(connector_follow, "follow_in_open_sessions", lambda _app: [])
    return calls


def test_enabling_an_enabled_connector_again_restarts_nothing(
    applied: dict[str, list[Any]],
) -> None:
    app = _app()
    asyncio.run(service.enable(app, "fixture-plain"))
    assert applied["refresh"] == [True]
    assert applied["probe"] == [True]

    # SABOTAGE: drop the unchanged check in ``apply`` and the second enable refreshes the
    # gateway again (every running server restarts) and starts a probe server.
    row = asyncio.run(service.enable(app, "fixture-plain"))
    assert applied["refresh"] == [True]
    assert applied["probe"] == [True]
    assert applied["rebind"] == ["fixture-plain", "fixture-plain"]
    assert row["state"]["status"] == "ready"


def test_after_a_restart_enabling_again_checks_but_restarts_nothing(
    applied: dict[str, list[Any]],
) -> None:
    app = _app()
    asyncio.run(service.enable(app, "fixture-plain"))
    service._probes(app).clear()  # a new server process has no checks yet
    asyncio.run(service.enable(app, "fixture-plain"))
    assert applied["refresh"] == [True]
    assert applied["probe"] == [True, True]


def test_a_changed_value_is_rebound_and_checked_without_a_reload(
    applied: dict[str, list[Any]],
) -> None:
    app = _app()
    asyncio.run(service.enable(app, "fixture-plain"))
    # The connector's values, secrets or sign-in changed since its last check: the saved
    # connection is a template, so the running servers take the values by rebind.
    service._probes(app)["fixture-plain"]["key"] = "an older configuration"
    asyncio.run(service.enable(app, "fixture-plain"))
    assert applied["refresh"] == [True]
    assert applied["probe"] == [True, True]
    assert applied["rebind"] == ["fixture-plain", "fixture-plain"]


def test_a_server_that_failed_its_check_is_checked_again(
    applied: dict[str, list[Any]],
) -> None:
    app = _app()
    asyncio.run(service.enable(app, "fixture-plain"))
    service._probes(app)["fixture-plain"]["status"] = "unreachable"
    asyncio.run(service.enable(app, "fixture-plain"))
    assert applied["refresh"] == [True]
    assert applied["probe"] == [True, True]


def test_other_applies_still_reload(applied: dict[str, list[Any]]) -> None:
    app = _app()
    asyncio.run(service.enable(app, "fixture-plain"))
    asyncio.run(service.apply(app, service.entry("fixture-plain")))
    assert applied["refresh"] == [True, True]
