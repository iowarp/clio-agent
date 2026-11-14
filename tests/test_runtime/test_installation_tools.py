"""A scoped installation's clio-kit reaches the agent's PATH however it was launched."""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from clio_agent.runtime import installation_tools
from clio_agent.runtime.installation_tools import adopt_installation_tools, installation_prefix
from clio_agent.runtime.mcp_launcher import probe_mcp_launchers
from clio_agent.tools.mcp_config import MCPServerSpec
from clio_agent.tools.mcp_prepare import launcher_interpreter


def _kit(prefix: Path, name: str = "clio-kit") -> Path:
    bin_dir = prefix / "uv-tools" / "bin"
    bin_dir.mkdir(parents=True)
    launcher = bin_dir / name
    launcher.write_text("#!/bin/sh\n", encoding="utf-8")
    launcher.chmod(0o755)
    return bin_dir


def test_prefix_is_the_folder_holding_the_running_venv(tmp_path: Path) -> None:
    venv = tmp_path / "data" / "app" / "clio-agent" / ".venv"

    assert installation_prefix(str(venv)) == tmp_path / "data" / "app"
    assert installation_prefix(str(tmp_path / "runtime")) is None
    assert installation_prefix(str(tmp_path / "other" / ".venv")) is None


def test_scoped_clio_kit_goes_first_on_path_with_its_tool_folders(tmp_path: Path) -> None:
    bin_dir = _kit(tmp_path)
    env = {"PATH": os.pathsep.join(["/usr/bin", str(bin_dir), "/bin"])}

    assert adopt_installation_tools(env, prefix=tmp_path) == bin_dir
    assert env["PATH"].split(os.pathsep) == [str(bin_dir), "/usr/bin", "/bin"]
    assert env["UV_TOOL_DIR"] == str(tmp_path / "uv-tools")
    assert env["UV_TOOL_BIN_DIR"] == str(bin_dir)
    # Idempotent: a second start does not grow PATH.
    adopt_installation_tools(env, prefix=tmp_path)
    assert env["PATH"].split(os.pathsep) == [str(bin_dir), "/usr/bin", "/bin"]


def test_installation_without_its_own_clio_kit_is_left_alone(tmp_path: Path) -> None:
    env = {"PATH": "/usr/bin", "UV_TOOL_DIR": "/mine"}

    assert adopt_installation_tools(env, prefix=tmp_path) is None
    assert env == {"PATH": "/usr/bin", "UV_TOOL_DIR": "/mine"}


def test_not_an_installed_venv_is_left_alone(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(installation_tools.sys, "prefix", "/opt/runtime")
    env = {"PATH": "/usr/bin"}

    assert adopt_installation_tools(env) is None
    assert env == {"PATH": "/usr/bin"}


def test_windows_layout_uses_the_exe_launcher(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    bin_dir = _kit(tmp_path, "clio-kit.exe")
    env = {"PATH": "/usr/bin"}
    monkeypatch.setattr(installation_tools.os, "name", "nt")

    assert adopt_installation_tools(env, prefix=tmp_path) == bin_dir
    assert env["PATH"].split(os.pathsep)[0] == str(bin_dir)


@pytest.mark.skipif(os.name == "nt", reason="a POSIX console script stands in for clio-kit")
def test_adopted_folder_clears_the_degraded_launcher_and_finds_its_interpreter(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The #739 symptom: ``mcp_launcher:clio-kit`` degraded on a desktop install."""

    prefix = tmp_path / "data" / "app"
    bin_dir = _kit(prefix)
    tool_python = prefix / "uv-tools" / "clio-kit" / "bin" / "python"
    tool_python.parent.mkdir(parents=True)
    tool_python.write_text("", encoding="utf-8")
    monkeypatch.setenv("PATH", "/usr/bin:/bin")
    monkeypatch.delenv("UV_TOOL_DIR", raising=False)
    monkeypatch.delenv("UV_TOOL_BIN_DIR", raising=False)
    monkeypatch.setattr(installation_tools.sys, "prefix", str(prefix / "clio-agent" / ".venv"))
    specs = {
        "adios": MCPServerSpec(
            name="adios",
            transport="stdio",
            command="clio-kit",
            args=("mcp-server", "adios"),
            source="plugin:adios",
        )
    }

    before = probe_mcp_launchers(specs=specs)
    adopt_installation_tools()
    after = probe_mcp_launchers(specs=specs)

    assert [item.name for item in before] == ["mcp_launcher:clio-kit"]
    assert after == []
    assert launcher_interpreter(str(bin_dir / "clio-kit")) == str(tool_python)
