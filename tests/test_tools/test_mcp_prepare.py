"""MCP server preparation is separate from its start (B3).

Pins: a ``uv run --project`` server runs from CLIO's environment, never the plugin
folder's ``.venv``; provisioning is its own short command whose inputs are recorded so
an unchanged environment is never rebuilt; failures are typed; clio-kit preparation is
used only when the installed launcher offers it; and every preparation has one visible,
waitable state.
"""

from __future__ import annotations

import os
import stat
import threading
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from clio_agent.tools import mcp_prepare
from clio_agent.tools.mcp_config import MCPServerSpec


@pytest.fixture(autouse=True)
def _cache_root(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    cache = tmp_path / "cache"
    monkeypatch.setattr("clio_agent.paths.user_cache_dir", lambda: cache)
    yield cache
    for namespace in ("spot", "geo", "a", "b"):
        mcp_prepare.forget(namespace)


@pytest.fixture
def _unconfined(monkeypatch: pytest.MonkeyPatch) -> None:
    """Run the provisioning command as composed (the fence is covered elsewhere)."""

    def wrap(command: str, args: list[str], **_kw: Any) -> Any:
        return SimpleNamespace(
            command=command, args=list(args), env_overlay={}, popen_kwargs={}, release=lambda: None
        )

    monkeypatch.setattr("clio_agent.runtime.sandbox.wrap_confined", wrap)
    monkeypatch.setattr("clio_agent.runtime.sandbox.effective_write_roots", lambda *a, **k: ())


def _uv_spec(project: Path, *extra: str, env: dict[str, str] | None = None, uv: str = "uv") -> Any:
    return MCPServerSpec(
        name="spot",
        transport="stdio",
        command=uv,
        args=("run", *extra, "--project", str(project), "spot-mcp", "--flag"),
        env=env or {},
    )


#: The fake launchers below are POSIX shell scripts.
posix_only = pytest.mark.skipif(os.name != "posix", reason="POSIX shell launchers")


def _fake_launcher(directory: Path, name: str, body: str) -> Path:
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / name
    path.write_text("#!/bin/sh\n" + body + "\n", encoding="utf-8")
    path.chmod(path.stat().st_mode | stat.S_IEXEC)
    return path


# --------------------------------------------------------------------------- #
# Where a uv project's environment lives                                      #
# --------------------------------------------------------------------------- #


def test_a_uv_project_server_runs_from_clios_environment(tmp_path: Path, _cache_root: Path) -> None:
    project = tmp_path / "plugin" / "impl"
    spec = _uv_spec(project)

    env = mcp_prepare.uv_project_environment(spec.command, spec.args, spec.env)

    target = Path(env["UV_PROJECT_ENVIRONMENT"])
    assert target.parent == _cache_root / "mcp-project-envs"
    assert target.name.startswith("plugin-"), "named after the plugin, not 'impl'"
    assert not str(target).startswith(str(project)), "never inside the plugin folder"
    # Stable: every spawn of the server and its provisioning agree on it.
    assert mcp_prepare.uv_project_environment(spec.command, spec.args, spec.env) == env


def test_the_spawn_environment_carries_the_redirect(tmp_path: Path) -> None:
    from clio_agent.tools.mcp_environment import stdio_environment

    spec = _uv_spec(tmp_path / "proj")
    env = stdio_environment(spec.env, command=spec.command, args=spec.args)

    assert env["UV_PROJECT_ENVIRONMENT"] == str(mcp_prepare.project_environment_dir(spec.args[2]))


@pytest.mark.parametrize(
    "case",
    ["declared", "no_sync", "not_uv"],
)
def test_the_redirect_leaves_declared_and_binding_environments_alone(
    case: str, tmp_path: Path
) -> None:
    project = tmp_path / "proj"
    if case == "declared":
        spec = _uv_spec(project, env={"UV_PROJECT_ENVIRONMENT": "/own/env"})
    elif case == "no_sync":
        spec = _uv_spec(project, "--no-sync")
    else:
        spec = MCPServerSpec(name="geo", transport="stdio", command="clio-kit", args=("x",))
    assert mcp_prepare.uv_project_environment(spec.command, spec.args, spec.env) == {}


# --------------------------------------------------------------------------- #
# Provisioning plans                                                          #
# --------------------------------------------------------------------------- #


def test_uv_sync_keeps_only_the_options_sync_understands(tmp_path: Path) -> None:
    spec = _uv_spec(tmp_path / "p", "--with", "extra-pkg", "--frozen", "--no-dev", "-q")

    plan = mcp_prepare.provision_plan(spec, "/bin/uv")

    assert isinstance(plan, mcp_prepare.ProvisionPlan)
    assert plan.argv == (
        "/bin/uv",
        "sync",
        "--frozen",
        "--no-dev",
        "--project",
        str(tmp_path / "p"),
    )
    assert plan.env["UV_PROJECT_ENVIRONMENT"] == plan.marker_dir
    assert plan.fingerprint


def test_a_no_sync_launcher_has_nothing_to_provision(tmp_path: Path) -> None:
    spec = _uv_spec(tmp_path / "p", "--no-sync")
    assert mcp_prepare.provision_plan(spec, "/bin/uv") == mcp_prepare.PROVISION_NOT_APPLICABLE


def test_clio_kit_prepares_only_when_the_launcher_offers_it(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    spec = MCPServerSpec(
        name="geo", transport="stdio", command="clio-kit", args=("mcp-server", "geo")
    )
    monkeypatch.setattr(mcp_prepare, "launcher_supports_prepare", lambda command: False)
    assert (
        mcp_prepare.provision_plan(spec, "/bin/clio-kit")
        == mcp_prepare.PROVISION_UNSUPPORTED_LAUNCHER
    )

    monkeypatch.setattr(mcp_prepare, "launcher_supports_prepare", lambda command: True)
    plan = mcp_prepare.provision_plan(spec, "/bin/clio-kit")
    assert isinstance(plan, mcp_prepare.ProvisionPlan)
    assert plan.argv == ("/bin/clio-kit", "mcp-server", "--prepare", "geo")


@posix_only
def test_prepare_support_is_read_from_the_launchers_help(tmp_path: Path) -> None:
    new = _fake_launcher(tmp_path / "new", "clio-kit", "echo '  --prepare  Build only'")
    old = _fake_launcher(tmp_path / "old", "clio-kit", "echo '  -b, --branch TEXT'")

    assert mcp_prepare.launcher_supports_prepare(str(new)) is True
    assert mcp_prepare.launcher_supports_prepare(str(old)) is False


def test_any_other_launcher_has_nothing_to_provision() -> None:
    spec = MCPServerSpec(name="geo", transport="stdio", command="node", args=("server.js",))
    assert mcp_prepare.provision_plan(spec, "/bin/node") == mcp_prepare.PROVISION_NOT_APPLICABLE


# --------------------------------------------------------------------------- #
# Provisioning runs                                                           #
# --------------------------------------------------------------------------- #


@posix_only
@pytest.mark.usefixtures("_unconfined")
def test_provisioning_runs_once_then_reads_its_marker(tmp_path: Path) -> None:
    calls = tmp_path / "calls.txt"
    uv = _fake_launcher(
        tmp_path / "bin", "uv", f'echo "$@" >> {calls}\nmkdir -p "$UV_PROJECT_ENVIRONMENT"'
    )
    project = tmp_path / "plugin" / "impl"
    project.mkdir(parents=True)
    (project / "pyproject.toml").write_text("[project]\nname='x'\n", encoding="utf-8")
    spec = _uv_spec(project, uv=str(uv))

    assert mcp_prepare.provision("spot", spec) == mcp_prepare.PROVISIONED
    assert mcp_prepare.provision("spot", spec) == mcp_prepare.PROVISION_CACHED
    assert calls.read_text(encoding="utf-8").count("sync") == 1
    assert not (project / ".venv").exists(), "nothing is written into the plugin folder"

    # A changed project is provisioned again.
    (project / "pyproject.toml").write_text("[project]\nname='y'\n", encoding="utf-8")
    assert mcp_prepare.provision("spot", spec) == mcp_prepare.PROVISIONED
    assert calls.read_text(encoding="utf-8").count("sync") == 2


@posix_only
@pytest.mark.usefixtures("_unconfined")
def test_a_failed_provision_is_typed_with_the_commands_output(tmp_path: Path) -> None:
    uv = _fake_launcher(tmp_path / "bin", "uv", "echo 'error: no solution' >&2\nexit 2")
    project = tmp_path / "proj"
    project.mkdir()
    spec = _uv_spec(project, uv=str(uv))

    with pytest.raises(mcp_prepare.PrepareError) as raised:
        mcp_prepare.provision("spot", spec)

    assert raised.value.reason == mcp_prepare.PROVISION_FAILED
    assert "exited 2" in raised.value.detail
    assert "no solution" in raised.value.detail
    # A failure leaves no marker: the next preparation tries again.
    assert not any(Path(mcp_prepare.project_environment_dir(str(project))).glob("*.json"))


def test_a_missing_launcher_is_typed(tmp_path: Path) -> None:
    spec = _uv_spec(tmp_path / "p", uv=str(tmp_path / "nowhere" / "uv"))
    with pytest.raises(mcp_prepare.PrepareError) as raised:
        mcp_prepare.provision("spot", spec)
    assert raised.value.reason == mcp_prepare.LAUNCHER_MISSING


def test_a_remote_server_has_nothing_to_provision() -> None:
    spec = MCPServerSpec(name="geo", transport="http", url="https://example.invalid/mcp")
    assert mcp_prepare.provision("geo", spec) == mcp_prepare.PROVISION_NOT_APPLICABLE


def test_failures_are_classified_by_phase() -> None:
    from clio_agent.tools.mcp_config import MCPSpawnError
    from clio_agent.tools.mcp_server_progress import NoProgressTimeout

    stall = NoProgressTimeout("connect", "no_progress", 30.0, 30.0)
    assert mcp_prepare.classify(stall, "provision") == mcp_prepare.PROVISION_NO_PROGRESS
    assert mcp_prepare.classify(stall, "launch") == mcp_prepare.START_NO_PROGRESS
    assert mcp_prepare.classify(MCPSpawnError("x"), "launch") == mcp_prepare.LAUNCHER_MISSING
    assert mcp_prepare.classify(ConnectionError("x"), "connect") == mcp_prepare.START_FAILED
    assert set(mcp_prepare.reason_catalog()) >= {
        mcp_prepare.PROVISION_FAILED,
        mcp_prepare.START_FAILED,
        mcp_prepare.PROVISION_UNSUPPORTED_LAUNCHER,
    }


# --------------------------------------------------------------------------- #
# Visible state                                                               #
# --------------------------------------------------------------------------- #


def test_state_moves_through_phases_and_a_new_preparation_restarts_its_clock() -> None:
    mcp_prepare.record("geo", mcp_prepare.PREPARING, mcp_prepare.PHASE_PROVISION)
    mcp_prepare.record("geo", mcp_prepare.PREPARING, mcp_prepare.PHASE_LAUNCH, provision="x")
    first = mcp_prepare.preparation_state("geo")
    assert first is not None
    assert (first["state"], first["phase"], first["provision"]) == ("preparing", "launch", "x")

    mcp_prepare.record("geo", mcp_prepare.FAILED, "launch", reason="r", detail="d")
    failed = mcp_prepare.preparation_state("geo")
    assert failed is not None and (failed["reason"], failed["detail"]) == ("r", "d")

    mcp_prepare.record("geo", mcp_prepare.PREPARING, mcp_prepare.PHASE_PROVISION)
    again = mcp_prepare.preparation_state("geo")
    assert again is not None
    assert "reason" not in again and "provision" not in again
    assert again["started_at"] >= failed["started_at"]


def test_a_waiter_returns_when_the_preparation_finishes() -> None:
    mcp_prepare.record("a", mcp_prepare.PREPARING, mcp_prepare.PHASE_LAUNCH)
    timer = threading.Timer(
        0.05, mcp_prepare.record, args=("a", mcp_prepare.READY, "launch"), kwargs={"tool_count": 2}
    )
    timer.start()
    try:
        rows = mcp_prepare.wait_for_namespaces(["a", "b"], 30.0)
    finally:
        timer.cancel()
    assert rows["a"] is not None and rows["a"]["state"] == "ready" and rows["a"]["tool_count"] == 2
    assert rows["b"] is None, "a namespace never prepared is reported, not waited for"


def test_a_waiter_gives_up_at_its_timeout() -> None:
    mcp_prepare.record("a", mcp_prepare.PREPARING, mcp_prepare.PHASE_LAUNCH)
    rows = mcp_prepare.wait_for_namespaces(["a"], 0.05)
    assert rows["a"] is not None and rows["a"]["state"] == "preparing"
    assert mcp_prepare.is_preparing("a")
    mcp_prepare.forget("a")
    assert mcp_prepare.preparation_state("a") is None


def test_listing_observers_get_live_listings_and_a_key_replaces_its_observer() -> None:
    seen: list[tuple[str, list[str]]] = []
    mcp_prepare.observe_listings("t1", lambda ns, tools: seen.append(("old", sorted(tools))))
    mcp_prepare.observe_listings("t1", lambda ns, tools: seen.append((ns, sorted(tools))))
    try:
        mcp_prepare.notify_listed("geo", {"geo_x": object()})
    finally:
        mcp_prepare.observe_listings("t1", lambda ns, tools: None)
    assert seen == [("geo", ["geo_x"])]
