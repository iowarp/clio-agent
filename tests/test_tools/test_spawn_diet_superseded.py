"""A dieted transport whose plan is retired spawns the declared command (VF-D9).

Found live (geo-globe stream): a plan was applied when the server's transport was built
(environment A); the same launch's ``clio-kit mcp-server --prepare`` then built
environment B and removed A. The transport kept A's interpreter as its argv, so every
spawn failed ("Failed to execvp …/A/bin/python") and every call answered "Connection
closed" until CLIO restarted, even after ``spawn_failed`` dropped the plan.
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

import pytest
from fastmcp.client.transports import StdioTransport

from clio_agent.tools import spawn_diet, spawn_diet_transport
from clio_agent.tools.mcp_config import MCPServerSpec, transport_for
from clio_agent.tools.mcp_environment_repair import interpreter_path
from tests.test_tools.test_spawn_diet import _fake_env, _store_plan

SPEC = MCPServerSpec(
    name="pandas", transport="stdio", command="clio-kit", args=("mcp-server", "pandas")
)


@pytest.fixture(autouse=True)
def _isolated_state(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(spawn_diet, "_cache_path", lambda: tmp_path / "diet.json")
    monkeypatch.setenv("CLIO_KIT_CACHE_DIR", str(tmp_path))
    monkeypatch.setattr(spawn_diet, "_scans_scheduled", set())
    monkeypatch.setattr(spawn_diet, "_pending_learns", {})
    monkeypatch.setattr(spawn_diet, "_applied_plans", {})


def _dieted_transport(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> tuple[Any, Path, str]:
    """A transport built from a valid plan into environment A, as ``transport_for`` does."""
    shim, entry = _fake_env(tmp_path)
    environment = shim.parent.parent
    (environment / "pyvenv.cfg").write_text("home = /usr/bin\n")
    interpreter = interpreter_path(environment)
    interpreter.parent.mkdir(exist_ok=True)
    if not interpreter.exists():
        interpreter.write_text("stub")
    launcher = tmp_path / "clio-kit.exe"
    launcher.write_text("launcher")
    monkeypatch.setattr("shutil.which", lambda cmd: str(launcher))
    _store_plan(str(launcher), [str(shim), str(entry)])
    transport = transport_for(SPEC, cwd=str(tmp_path))
    assert isinstance(transport, spawn_diet_transport.DietStdioTransport)
    assert transport.command == str(shim)
    return transport, environment, str(launcher)


def _spawned(monkeypatch: pytest.MonkeyPatch) -> list[tuple[str, list[str]]]:
    """Record each spawn's argv instead of starting a process."""
    spawned: list[tuple[str, list[str]]] = []

    async def connect(self: StdioTransport, **_kwargs: Any) -> None:
        spawned.append((self.command, list(self.args)))

    monkeypatch.setattr(StdioTransport, "connect", connect)
    return spawned


def test_prepare_reporting_another_environment_drops_the_plan_and_respawns_declared(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """SABOTAGE: skip ``environment_prepared`` in ``record_prepared`` and the spawn
    execs environment A's interpreter."""
    transport, environment, launcher = _dieted_transport(tmp_path, monkeypatch)
    events: list[str] = []
    monkeypatch.setattr(
        spawn_diet_transport.trace, "event", lambda _c, fmt, *a: events.append(fmt % a)
    )
    monkeypatch.setattr(spawn_diet.trace, "event", lambda _c, fmt, *a: events.append(fmt % a))
    spawned = _spawned(monkeypatch)

    # Prepare names the same environment: the plan stays.
    spawn_diet_transport.record_prepared("pandas", {"status": "ok", "environment": environment})
    assert spawn_diet._load_cache() != {}
    # Prepare built environment B (a new source hash) and replaced A.
    other = tmp_path / "mcp-environments" / "pandas-0123456789abcdef01234567"
    spawn_diet_transport.record_prepared("pandas", {"status": "prepared", "environment": other})
    assert spawn_diet._load_cache() == {}
    assert any("reason=env_superseded" in event for event in events)

    asyncio.run(transport.connect())
    assert spawned == [(launcher, ["mcp-server", "pandas"])]
    assert any(f"reason={spawn_diet_transport.TRANSPORT_REDECLARED}" in e for e in events)
    assert transport.plan_key is None  # declared now: nothing left to re-check


def test_after_a_failed_dieted_spawn_the_next_spawn_is_declared(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """SABOTAGE: drop the staleness check in ``DietStdioTransport.connect`` and the
    second spawn execs the dead interpreter again."""
    transport, _environment, launcher = _dieted_transport(tmp_path, monkeypatch)
    spawned = _spawned(monkeypatch)
    asyncio.run(transport.connect())
    assert spawned[0][0] == transport.command != launcher  # the plan's interpreter

    spawn_diet.spawn_failed("pandas")  # the executor's hook for a failed connect
    asyncio.run(transport.connect())
    assert spawned[1] == (launcher, ["mcp-server", "pandas"])


def test_a_transport_without_a_plan_is_the_rooted_stdio_transport(tmp_path: Path) -> None:
    rooted = spawn_diet_transport.RootedStdioTransport
    assert spawn_diet_transport.stdio_transport(SPEC, str(tmp_path), False) is rooted
    assert spawn_diet.environment_prepared("pandas", str(tmp_path)) is False  # no plan


def test_a_spawn_recreates_a_workspace_root_deleted_after_mounting(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """SABOTAGE: drop ``recreate_root`` in ``RootedStdioTransport.connect`` and the spawn
    runs in a folder that no longer exists (ENOENT, the whole turn fails)."""
    root = tmp_path / "root"
    root.mkdir()
    transport = transport_for(
        MCPServerSpec(name="plain", transport="stdio", command="sh"), cwd=str(root)
    )
    assert isinstance(transport, spawn_diet_transport.RootedStdioTransport)
    root.rmdir()
    seen: list[bool] = []

    async def connect(self: StdioTransport, **_kwargs: Any) -> None:
        seen.append(Path(str(self.cwd)).is_dir())

    monkeypatch.setattr(StdioTransport, "connect", connect)
    asyncio.run(transport.connect())
    assert seen == [True]
