"""One configured default port (``gact.port``) for the server, its launcher and its clients."""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest

from clio_agent import server_port
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


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX launcher")
@pytest.mark.parametrize(
    ("config", "environment", "expected"),
    [
        (None, None, "17800"),
        (None, "18123", "18123"),
        ("gact:\n  port: 18456\n", "18123", "18456"),
    ],
)
def test_launcher_uses_the_installed_configuration(
    tmp_path: Path, config: str | None, environment: str | None, expected: str
) -> None:
    prefix = tmp_path / "prefix"
    venv_bin = prefix / "clio-agent" / ".venv" / "bin"
    venv_bin.mkdir(parents=True)
    python = venv_bin / "python"
    python.write_text(f'#!/bin/sh\nexec "{sys.executable}" "$@"\n', encoding="utf-8")
    python.chmod(0o755)
    config_dir = tmp_path / "config"
    config_dir.mkdir()
    if config is not None:
        (config_dir / "config.yaml").write_text(config, encoding="utf-8")
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

    result = subprocess.run(
        ["bash", str(LAUNCHER), "status"],
        cwd=tmp_path,
        env=env,
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )

    assert f"port:    {expected}\n" in result.stdout, result.stdout + result.stderr
