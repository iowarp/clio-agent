"""One configured default port (``gact.port``) for the server, its launcher and its clients."""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest

from clio_agent import conf, server_port
from tests._config_layer import delete_config, set_config

LAUNCHER = Path(__file__).resolve().parents[2] / "install" / "clio"


def test_default_is_17800(monkeypatch: pytest.MonkeyPatch) -> None:
    delete_config("gact.port")
    monkeypatch.delenv("CLIO_PORT", raising=False)

    assert server_port.configured_port() == 17800


def test_environment_sets_it_when_the_file_does_not(monkeypatch: pytest.MonkeyPatch) -> None:
    delete_config("gact.port")
    monkeypatch.setenv("CLIO_PORT", "18123")

    assert server_port.configured_port() == 18123


def test_config_file_wins_over_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    set_config("gact.port", 18456)
    monkeypatch.setenv("CLIO_PORT", "18123")

    assert server_port.configured_port() == 18456


def test_a_workspace_config_never_moves_the_machine_wide_port(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """One server per machine: the directory a command runs from cannot change its port."""
    set_config("gact.port", 18456)
    workspace = tmp_path / "project"
    (workspace / ".clio-agent" / "shared").mkdir(parents=True)
    (workspace / ".clio-agent" / "shared" / "config.yaml").write_text("gact:\n  port: 18999\n")
    monkeypatch.chdir(workspace)
    conf.reload()

    assert server_port.configured_port() == 18456
    delete_config("gact.port")
    monkeypatch.setenv("CLIO_PORT", "18123")
    assert server_port.configured_port() == 18123


def test_the_helper_prints_only_the_port_from_any_directory(tmp_path: Path) -> None:
    """Desktop and web run ``python -I -m clio_agent.server_port`` from a scratch cwd."""
    config_dir = tmp_path / "config"
    config_dir.mkdir()
    (config_dir / "config.yaml").write_text("gact:\n  port: 18456\n", encoding="utf-8")
    scratch = tmp_path / "scratch"
    (scratch / ".clio-agent" / "shared").mkdir(parents=True)
    (scratch / ".clio-agent" / "shared" / "config.yaml").write_text("gact:\n  port: 18999\n")
    env = {
        "PATH": os.environ["PATH"],
        "HOME": str(tmp_path),
        "CLIO_AGENT_CONFIG_DIR": str(config_dir),
        "CLIO_PORT": "18123",
    }
    result = subprocess.run(
        [sys.executable, "-I", "-m", "clio_agent.server_port"],
        cwd=scratch,
        env=env,
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )

    assert (result.returncode, result.stdout) == (0, "18456\n"), result.stderr


def test_out_of_range_port_is_refused(monkeypatch: pytest.MonkeyPatch) -> None:
    set_config("gact.port", 70000)

    with pytest.raises(ValueError, match="gact.port"):
        server_port.configured_port()


def test_server_drops_clio_port_once_resolved(monkeypatch: pytest.MonkeyPatch) -> None:
    """clio-core reads CLIO_PORT as its own RPC port; the API's value must not leak to it."""
    from clio_agent.gact import app as gact_app
    from clio_agent.gact import desktop_lifecycle

    delete_config("gact.port")
    monkeypatch.setenv("CLIO_PORT", "18789")
    seen: dict[str, object] = {}

    def fake_serve(app: object, *, host: str, port: int) -> None:
        seen.update(port=port, leaked=os.environ.get("CLIO_PORT"))

    monkeypatch.setattr(gact_app, "build_app", lambda: type("A", (), {"state": _State()})())
    monkeypatch.setattr(gact_app.server_boot, "mark_real_server", lambda app: None)
    monkeypatch.setattr(desktop_lifecycle, "serve_foreground", fake_serve)
    monkeypatch.setattr("clio_agent.home_relocation.relocate_home", lambda: None)

    gact_app.run_server(no_agent=True)

    assert seen == {"port": 18789, "leaked": None}


class _State:
    pass


def _launcher(
    tmp_path: Path, args: list[str], *, config: str | None, environment: str | None
) -> subprocess.CompletedProcess[str]:
    prefix = tmp_path / "prefix"
    venv_bin = prefix / "clio-agent" / ".venv" / "bin"
    venv_bin.mkdir(parents=True, exist_ok=True)
    python = venv_bin / "python"
    python.write_text(f'#!/bin/sh\nexec "{sys.executable}" "$@"\n', encoding="utf-8")
    python.chmod(0o755)
    config_dir = tmp_path / "config"
    config_dir.mkdir(exist_ok=True)
    if config is not None:
        (config_dir / "config.yaml").write_text(config, encoding="utf-8")
    # A workspace config in the launcher's cwd never sets the machine-wide port.
    (tmp_path / ".clio-agent" / "shared").mkdir(parents=True, exist_ok=True)
    (tmp_path / ".clio-agent" / "shared" / "config.yaml").write_text("gact:\n  port: 18999\n")
    env = {
        "PATH": os.environ["PATH"],
        "HOME": str(tmp_path),
        "CLIO_PREFIX": str(prefix),
        "CLIO_AGENT_CONFIG_DIR": str(config_dir),
        "CLIO_AGENT_STATE_DIR": str(tmp_path / "state"),
        "CLIO_AGENT_CACHE_DIR": str(tmp_path / "cache"),
        "CLIO_NO_UPDATE_CHECK": "1",
    }
    if environment is not None:
        env["CLIO_PORT"] = environment
    return subprocess.run(
        ["bash", str(LAUNCHER), *args],
        cwd=tmp_path,
        env=env,
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX launcher")
@pytest.mark.parametrize(
    ("args", "config", "environment", "expected"),
    [
        (["status"], None, None, "17800"),
        (["status"], None, "18123", "18123"),
        (["status"], "gact:\n  port: 18456\n", "18123", "18456"),
        (["--port", "18777", "status"], "gact:\n  port: 18456\n", "18123", "18777"),
        (["status", "--port=18778"], "gact:\n  port: 18456\n", None, "18778"),
    ],
)
def test_launcher_port_precedence(
    tmp_path: Path, args: list[str], config: str | None, environment: str | None, expected: str
) -> None:
    """--port, then the user config.yaml gact.port, then CLIO_PORT, then 17800."""
    result = _launcher(tmp_path, args, config=config, environment=environment)

    assert f"port:    {expected}\n" in result.stdout, result.stdout + result.stderr


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX launcher")
@pytest.mark.parametrize("value", ["0", "70000", "http", ""])
def test_launcher_refuses_a_port_that_is_not_one(tmp_path: Path, value: str) -> None:
    result = _launcher(tmp_path, ["--port", value, "status"], config=None, environment=None)

    assert result.returncode == 2 and "--port" in result.stderr, result.stdout + result.stderr


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX launcher")
def test_launcher_passes_other_commands_arguments_through(tmp_path: Path) -> None:
    """A passthrough command's own ``--port`` belongs to it, not to the launcher."""
    result = _launcher(tmp_path, ["completion", "--port"], config=None, environment=None)

    assert "usage: clio completion bash|zsh" in result.stderr, result.stdout + result.stderr


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX shell")
@pytest.mark.parametrize(("marked", "expected"), [(True, "--port 18555 start"), (False, "start")])
def test_remote_deploys_pass_the_port_to_launchers_that_take_it(
    tmp_path: Path, marked: bool, expected: str
) -> None:
    from clio_agent.gact.infrastructure.clio_agent_deploy import CLIO_LAUNCHER

    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    marker = "# clio-launcher-feature: port-flag\n" if marked else ""
    (bin_dir / "clio").write_text(f'#!/bin/sh\n{marker}echo "$@"\n', encoding="utf-8")
    (bin_dir / "clio").chmod(0o755)
    result = subprocess.run(
        [
            "bash",
            "-c",
            f'bin="$1"; export CLIO_PORT=18555; {CLIO_LAUNCHER} clio_launcher start',
            "x",
            str(bin_dir),
        ],
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )

    assert result.stdout == f"{expected}\n", result.stderr


def test_the_shipped_launcher_carries_the_port_flag_marker() -> None:
    assert "clio-launcher-feature: port-flag" in LAUNCHER.read_text(encoding="utf-8")
