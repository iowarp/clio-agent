"""A connector check never starts a second server of a namespace a conversation runs.

Dry 14: the check that counts a connector's tools started its own server while the
conversation's server of the same namespace was starting, so two ran at once. With one
node slot per sandbox the second pruned the first one's node, and the scene was lost.
"""

from __future__ import annotations

import asyncio
import dataclasses
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from clio_agent.connectors import materialize, state
from clio_agent.gact import connectors_service as service
from clio_agent.tools.mcp_config import spec_from_declaration

NAME = "fixture-plain"


class _Tool(SimpleNamespace):
    def model_dump(self, **_: Any) -> dict[str, Any]:
        return {"name": self.name, "inputSchema": {"type": "object"}}


class _External:
    def __init__(self, client: Any) -> None:
        self.client, self.asked = client, []

    async def live_client(self, server: str, owners: list[Any]) -> Any:
        self.asked.append((server, list(owners)))
        return self.client


class _Client:
    async def list_tools(self) -> list[_Tool]:
        return [_Tool(name="first"), _Tool(name="second")]


def _app(fleet_spec: Any, client: Any, proxy: Any) -> Any:
    executor = SimpleNamespace(
        _clio_namespace_specs={NAME: fleet_spec},
        _async_executor=SimpleNamespace(_namespace_servers={NAME: proxy}),
    )
    return SimpleNamespace(
        state=SimpleNamespace(
            agent=SimpleNamespace(tool_executor=executor),
            external_tasks=_External(client),
            mcp_app_loop=None,
        )
    )


@pytest.fixture
def started(fixture_catalog: Path, monkeypatch: pytest.MonkeyPatch) -> list[Any]:
    from clio_agent.gact.routes import mcp_configuration

    starts: list[Any] = []

    async def probe_server(declared: Any, *, details: bool = False) -> tuple[list, None]:
        starts.append(declared)
        return [{"name": "only"}], None

    monkeypatch.setattr(mcp_configuration, "_probe_user_mcp_server", probe_server)
    return starts


def _running_spec() -> Any:
    found = service.entry(NAME)
    declared = materialize.resolved(found, state.choice(NAME).values)
    spec = spec_from_declaration(NAME, dict(declared), source="plugin")
    env = {**(spec.env or {}), "PLUGIN_ROOT": "/plugins/x"}
    return dataclasses.replace(spec, env=env)


def test_the_running_server_answers_and_no_server_is_started(started: list[Any]) -> None:
    """SABOTAGE: skip ``_live_tools`` in ``probe`` -> a second server starts."""
    proxy = object()
    app = _app(_running_spec(), _Client(), proxy)
    result = asyncio.run(service.probe(app, service.entry(NAME), state.choice(NAME), force=True))
    assert started == []
    assert result["status"] == "ready"
    assert result["tool_count"] == 2
    assert app.state.external_tasks.asked == [(NAME, [proxy])]


def test_with_no_running_server_one_is_started_to_check(started: list[Any]) -> None:
    app = _app(_running_spec(), None, object())
    result = asyncio.run(service.probe(app, service.entry(NAME), state.choice(NAME), force=True))
    assert len(started) == 1
    assert result["tool_count"] == 1


def test_a_server_running_another_connection_is_not_asked(started: list[Any]) -> None:
    """A fleet whose namespace runs other values is not the checked connection.

    SABOTAGE: drop the ``_same_connection`` filter in ``live_owners`` -> it answers.
    """
    other = dataclasses.replace(_running_spec(), args=["--other"])
    app = _app(other, _Client(), object())
    asyncio.run(service.probe(app, service.entry(NAME), state.choice(NAME), force=True))
    assert len(started) == 1
    assert app.state.external_tasks.asked == []


def test_the_running_servers_failure_is_the_checks_outcome(started: list[Any]) -> None:
    class Broken:
        async def list_tools(self) -> list[Any]:
            raise RuntimeError("server went away")

    app = _app(_running_spec(), Broken(), object())
    result = asyncio.run(service.probe(app, service.entry(NAME), state.choice(NAME), force=True))
    assert started == []
    assert result["status"] == "unreachable"
    assert "server went away" in result["reason"]


def test_a_fleet_being_built_does_not_block_the_loop(started: list[Any]) -> None:
    """Live run 2a: a fleet built after a restart holds the workspace-fleet lock while
    it waits for the app loop to start its servers; a check that took that lock on the
    loop deadlocked the whole app.

    SABOTAGE: call ``live_owners`` on the loop in ``_live_tools`` -> the loop never
    runs the fleet's start, and this times out.
    """
    import threading

    lock = threading.Lock()
    app = _app(_running_spec(), _Client(), object())
    app.state.agent._workspace_state = lambda: (lock, {}, {})

    async def main() -> dict[str, Any]:
        loop = asyncio.get_running_loop()
        holding, built = threading.Event(), threading.Event()

        def build() -> None:
            with lock:  # the fleet resolve, waiting on the loop with the lock held
                holding.set()
                asyncio.run_coroutine_threadsafe(asyncio.sleep(0.2), loop).result()
                built.set()

        holder = threading.Thread(target=build)
        holder.start()
        await asyncio.to_thread(holding.wait, 10)
        checked = service.probe(app, service.entry(NAME), state.choice(NAME), force=True)
        result = await asyncio.wait_for(checked, 10)
        holder.join(10)
        assert built.is_set()
        return result

    assert asyncio.run(main())["tool_count"] == 2


def test_a_plain_server_on_its_proxy_route_answers_over_its_fleet(started: list[Any]) -> None:
    """Take openEMS: a server without tasks has no kept-alive task connection (its calls
    reach the proxy's backend), so the check started a second server beside it.

    SABOTAGE: drop the ``_fleet_tools`` read in ``_live_tools`` -> a server starts.
    """
    backend = SimpleNamespace(_connect_task=SimpleNamespace(done=lambda: False))
    proxy = SimpleNamespace(_clio_backend_transport=backend)
    app = _app(_running_spec(), None, proxy)
    listed: list[str] = []

    def list_namespace_tools(namespace: str) -> list[_Tool]:
        listed.append(namespace)
        return [_Tool(name="first"), _Tool(name="second"), _Tool(name="third")]

    app.state.agent.tool_executor.list_namespace_tools = list_namespace_tools
    result = asyncio.run(service.probe(app, service.entry(NAME), state.choice(NAME), force=True))
    assert started == []
    assert listed == [NAME]
    assert result["tool_count"] == 3


def test_concurrent_checks_share_one_server_start(
    fixture_catalog: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """SABOTAGE: start a check per caller in ``probe`` -> two servers start."""
    from clio_agent.gact.routes import mcp_configuration

    starts: list[Any] = []

    async def probe_server(declared: Any, *, details: bool = False) -> tuple[list, None]:
        starts.append(declared)
        await asyncio.sleep(0.05)
        return [{"name": "only"}], None

    monkeypatch.setattr(mcp_configuration, "_probe_user_mcp_server", probe_server)
    app = _app(_running_spec(), None, object())
    found, choice = service.entry(NAME), state.choice(NAME)

    async def main() -> list[dict[str, Any]]:
        return list(await asyncio.gather(*(service.probe(app, found, choice) for _ in range(2))))

    results = asyncio.run(main())
    assert len(starts) == 1
    assert [r["tool_count"] for r in results] == [1, 1]
    assert app.state.connector_probe_flights == {}


def test_a_check_in_flight_reads_as_starting_and_only_its_end_is_kept(
    fixture_catalog: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """KiCad: a first ``uvx`` start that took longer than a fixed limit was kept as
    "did not answer in time" though its server came up.

    SABOTAGE: cache the pending check in ``_check`` -> the row is a verdict too early.
    """
    from clio_agent.gact.routes import mcp_configuration

    gate: dict[str, asyncio.Event] = {}

    async def probe_server(declared: Any, *, details: bool = False) -> tuple[list, str]:
        await gate["release"].wait()
        return [], "McpError: Connection closed: kicad-mcp-pro: error: no such board"

    monkeypatch.setattr(mcp_configuration, "_probe_user_mcp_server", probe_server)
    state.update(NAME, enabled=True)
    app = _app(_running_spec(), None, object())
    found, choice = service.entry(NAME), state.choice(NAME)

    async def main() -> tuple[dict[str, Any], dict[str, Any]]:
        gate["release"] = asyncio.Event()
        service.check_soon(app, found, choice)
        await asyncio.sleep(0)
        pending = service.row(app, found, choice)["state"]
        assert found.id not in service._probes(app)
        gate["release"].set()
        await service.probe(app, found, choice)  # joins the check in flight
        return pending, service.row(app, found, choice)["state"]

    pending, ended = asyncio.run(main())
    assert pending["status"] == "starting" and "so far" in pending["reason"]
    assert ended["status"] == "unreachable"
    assert "no such board" in ended["reason"]
