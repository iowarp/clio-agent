"""Desktop ownership is scoped to one launch and one Desktop instance."""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from clio_agent.gact.infrastructure.drivers import build_driver_plan
from clio_agent.gact.infrastructure.models import CommandResult, CommandSpec, TargetFacts
from clio_agent.gact.infrastructure.remote_lifecycle import (
    RemoteLaunch,
    start_owned_command,
    stop_owned_command,
)
from clio_agent.gact.infrastructure.runtime import InfrastructureRuntime
from clio_agent.gact.infrastructure.store import InfrastructureStore
from clio_agent.gact.infrastructure.transport import InfrastructureTransportRegistry
from clio_agent.gact.routes.infrastructure import register_infrastructure_routes
from tests.test_gact.test_clio_agent_deploy import (
    _alive,
    _fake_install,
    _free_port,
    _run,
    _run_script,
    _ScriptedTransports,
    _start_clio,
    _target,
    linux_only,
)


@pytest.mark.asyncio
async def test_exit_stops_only_this_desktops_nonpersistent_launches(tmp_path: Path) -> None:
    """Adopted agents, another Desktop's agents and explicit persistence are spared."""

    store = InfrastructureStore(None)
    runtime = InfrastructureRuntime(store, InfrastructureTransportRegistry(lambda *_: None))
    targets = [_target(store).id for _ in range(4)]
    runtime._remote_launches = {
        targets[0]: RemoteLaunch("/owned", 17800, "launch-a", desktop_id="desktop-a"),
        targets[1]: RemoteLaunch("/persistent", 17801, "launch-b", True, "desktop-a"),
        targets[2]: RemoteLaunch("/other", 17802, "launch-c", desktop_id="desktop-b"),
    }
    stopped: list[tuple[str, CommandSpec]] = []

    async def execute(target_id: str, spec: CommandSpec) -> CommandResult:
        stopped.append((target_id, spec))
        return CommandResult(exit_code=0)

    runtime._execute = execute  # type: ignore[method-assign]
    assert await runtime.stop_desktop_agents("desktop-a") == []
    assert len(stopped) == 1
    assert stopped[0][0] == targets[0]
    assert stopped[0][1].args[-1] == "launch-a"
    assert await runtime.stop_desktop_agents("desktop-a") == []
    assert len(stopped) == 1


@pytest.mark.asyncio
async def test_exit_waits_for_inflight_launch_and_retains_failed_cleanup() -> None:
    """Drain waits for service settlement and reports a failed stop for retry."""

    store = InfrastructureStore(None)
    runtime = InfrastructureRuntime(store, InfrastructureTransportRegistry(lambda *_: None))
    target = _target(store)
    launch = RemoteLaunch("/owned", 17800, "launch-a", desktop_id="desktop-a")
    runtime._remote_launches[target.id] = launch
    calls: list[str] = []

    async def execute(target_id: str, spec: CommandSpec) -> CommandResult:
        calls.append(target_id)
        return CommandResult(exit_code=1, stderr="SSH disconnected")

    runtime._execute = execute  # type: ignore[method-assign]
    async with runtime._target_locks[target.id]:
        stop = asyncio.create_task(runtime.stop_desktop_agents("desktop-a"))
        await asyncio.sleep(0)
        assert not calls
        assert "desktop-a" in runtime._exiting_desktops
    failures = await stop
    assert failures == [f"{target.id}: SSH disconnected"]
    assert runtime._remote_launches[target.id] is launch


@pytest.mark.asyncio
async def test_explicit_reconnect_never_acquires_shutdown_ownership(tmp_path: Path) -> None:
    """Connecting to an existing process does not schedule it for stop on exit."""

    store = InfrastructureStore(None)
    target = _target(store)
    transports = _ScriptedTransports(
        "clio-deploy result=found existing_root=1 pid=321 installed=0.9.5b1 state=healthy owner=/existing\n"
    )
    operation = await _run(
        tmp_path,
        transports,
        store=store,
        target_id=target.id,
        configuration={"on_conflict": "connect", "desktop_id": "desktop-a"},
    )
    assert operation.state == "succeeded"
    record = store.service(target.id, "clio_agent")
    assert record is not None and record.configuration["desktop_owned"] == "false"
    assert "start" not in transports.ran


@pytest.mark.asyncio
async def test_matching_existing_agent_still_requires_an_explicit_choice(tmp_path: Path) -> None:
    """A found process is a question even when its version matches the controller."""

    from clio_agent.gact.infrastructure.drivers import clio_agent_version

    transports = _ScriptedTransports(
        f"clio-deploy result=found existing_root=1 pid=321 installed={clio_agent_version()} state=healthy owner=/existing\n"
    )
    operation = await _run(tmp_path, transports)
    assert operation.error == "clio_deploy_version_conflict"
    assert operation.conflict.target_version == operation.conflict.installed_version
    assert operation.conflict.owner == "/existing"
    assert "install" not in transports.ran and "start" not in transports.ran


@linux_only
@pytest.mark.parametrize("matching_token", [True, False])
def test_exit_verifies_the_real_process_launch_token(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, matching_token: bool
) -> None:
    """A pidfile alone cannot grant shutdown ownership of an existing CLIO."""

    prefix = tmp_path / "clio"
    _fake_install(prefix)
    port = _free_port()
    monkeypatch.setenv("CLIO_DESKTOP_LAUNCH", "original-launch")
    server = _start_clio(prefix, port)
    try:
        launch = RemoteLaunch(
            str(prefix), port, "original-launch" if matching_token else "new-launch"
        )
        result = _run_script(stop_owned_command(launch))
        if matching_token:
            assert result.returncode == 0, result.stdout
            server.wait(timeout=5)
            assert prefix.exists(), "exit keeps the installation and its data"
        else:
            assert result.returncode != 0
            assert "another launch" in result.stdout
            assert _alive(server)
    finally:
        if _alive(server):
            server.kill()
        server.wait(timeout=5)


@linux_only
def test_owned_launch_uses_the_shipped_launcher_and_stops_on_exit(tmp_path: Path) -> None:
    """The real launcher preserves the ownership token into the server process."""

    prefix = tmp_path / "clio"
    _fake_install(prefix)
    binary = prefix / "bin" / "clio"
    binary.parent.mkdir()
    binary.write_bytes((Path(__file__).parents[2] / "install" / "clio").read_bytes())
    binary.chmod(0o755)
    launch = RemoteLaunch(str(prefix), _free_port(), "desktop-launch-test")
    try:
        result = _run_script(start_owned_command(launch))
        assert result.returncode == 0, result.stdout + result.stderr
        assert "Started Desktop-owned CLIO" in result.stdout
    finally:
        result = _run_script(stop_owned_command(launch))
        assert result.returncode == 0, result.stdout + result.stderr
    assert prefix.exists()
    assert not list(prefix.glob("clio-server.*.pid"))


@linux_only
def test_replace_rechecks_the_pid_after_the_user_answers(tmp_path: Path) -> None:
    """An answer about the old process must not stop a replacement process."""

    from clio_agent.gact.infrastructure.clio_agent_deploy import claim_command

    prefix = tmp_path / "clio"
    _fake_install(prefix)
    port = _free_port()
    server = _start_clio(prefix, port)
    try:
        result = _run_script(
            claim_command(str(prefix), port, "0.9.5b1", replace=True, expected_pid="1")
        )
        assert result.returncode != 0
        assert _alive(server)
    finally:
        server.kill()
        server.wait(timeout=5)


@pytest.mark.parametrize("choice,root", [("update", "/existing"), ("replace", "/selected")])
def test_update_and_replace_target_the_documented_installation(choice: str, root: str) -> None:
    """Update keeps the existing root; replace uses the selected install location."""

    plan = build_driver_plan(
        service_id="clio_agent",
        action="install",
        variant_id="released",
        configuration={"port": "17801", "conflict_root": "/existing", "conflict_pid": "123"},
        facts=TargetFacts(target_id="t", label="Host", os="linux", arch="x86_64"),
        target=_target(InfrastructureStore(None), "/selected"),
        resolved_root="/previously-adopted",
        on_conflict=choice,
    )
    assert plan.remote_launch is not None and plan.remote_launch.root == root
    assert plan.connection_port == 17801
    assert plan.commands[0].args[-1] == "123"


def test_exit_requires_authentication_even_on_loopback(tmp_path: Path) -> None:
    """A website cannot stop the Desktop's agents through an unauthenticated POST."""

    app = FastAPI()
    app.state.bearer_token = "controller-secret"
    register_infrastructure_routes(app, tmp_path)
    with TestClient(app) as client:
        assert (
            client.post(
                "/v1/infrastructure/desktop-exit", json={"desktop_id": "desktop-a"}
            ).status_code
            == 401
        )
        response = client.post(
            "/v1/infrastructure/desktop-exit",
            json={"desktop_id": "desktop-a"},
            headers={"Authorization": "Bearer controller-secret"},
        )
        assert response.status_code == 200 and response.json() == {"failures": []}
