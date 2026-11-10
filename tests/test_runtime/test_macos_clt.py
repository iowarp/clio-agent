"""CLIO treats the macOS Command Line Tools stubs as missing tools."""

from __future__ import annotations

import os
import subprocess
from pathlib import Path
from typing import Any

import pytest

from clio_agent.runtime import execution_environment as execution
from clio_agent.runtime import macos_clt


@pytest.fixture
def no_command_line_tools(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """A Mac without the Command Line Tools whose shim binary is ``tmp_path/shim``."""
    shim = tmp_path / "shim"
    shim.write_text("#!/bin/sh\nexit 72\n", encoding="utf-8")
    shim.chmod(0o755)
    monkeypatch.setattr(macos_clt, "_SHIM", shim)
    monkeypatch.setattr(macos_clt, "_XCRUN", tmp_path / "absent-xcrun")
    monkeypatch.setattr(macos_clt, "command_line_tools_missing", lambda: True)
    return shim


def _stub(directory: Path, name: str, shim: Path) -> Path:
    directory.mkdir(parents=True, exist_ok=True)
    os.link(shim, directory / name)
    return directory / name


def _tool(directory: Path, name: str) -> Path:
    directory.mkdir(parents=True, exist_ok=True)
    tool = directory / name
    tool.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    tool.chmod(0o755)
    return tool


@pytest.mark.skipif(os.name == "nt", reason="POSIX executables and hard links")
def test_a_stub_is_skipped_for_the_next_real_tool_on_path(
    tmp_path: Path, no_command_line_tools: Path
) -> None:
    _stub(tmp_path / "usr-bin", "python3", no_command_line_tools)
    real = _tool(tmp_path / "later", "python3")
    path = os.pathsep.join([str(tmp_path / "usr-bin"), str(tmp_path / "later")])

    assert macos_clt.real_which("python3", path=path) == str(real)


@pytest.mark.skipif(os.name == "nt", reason="POSIX executables and hard links")
def test_a_stub_alone_is_a_missing_tool(tmp_path: Path, no_command_line_tools: Path) -> None:
    stub = _stub(tmp_path / "usr-bin", "git", no_command_line_tools)

    assert macos_clt.is_command_line_tools_stub(stub)
    assert macos_clt.real_which("git", path=str(tmp_path / "usr-bin")) is None
    assert macos_clt.real_which(str(stub)) is None


@pytest.mark.skipif(os.name == "nt", reason="POSIX executables and hard links")
def test_with_the_command_line_tools_the_stub_is_a_real_tool(
    tmp_path: Path, no_command_line_tools: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    stub = _stub(tmp_path / "usr-bin", "python3", no_command_line_tools)
    monkeypatch.setattr(macos_clt, "command_line_tools_missing", lambda: False)

    assert not macos_clt.is_command_line_tools_stub(stub)
    assert macos_clt.real_which("python3", path=str(tmp_path / "usr-bin")) == str(stub)


@pytest.mark.parametrize(
    ("platform", "returncode", "selected", "missing"),
    [
        ("darwin", 2, "", True),
        ("darwin", 0, "selected", False),
        ("darwin", 0, "vanished", True),
        ("linux", 2, "", False),
    ],
)
def test_xcode_select_decides_whether_the_tools_are_installed(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    platform: str,
    returncode: int,
    selected: str,
    missing: bool,
) -> None:
    (tmp_path / "selected").mkdir()
    output = str(tmp_path / selected) if selected else ""
    calls: list[list[str]] = []

    def run(command: list[str], **_kwargs: Any) -> subprocess.CompletedProcess[str]:
        calls.append(command)
        return subprocess.CompletedProcess(command, returncode, output + "\n", "")

    monkeypatch.setattr(macos_clt.sys, "platform", platform)
    monkeypatch.setattr(macos_clt.subprocess, "run", run)
    macos_clt.command_line_tools_missing.cache_clear()
    try:
        assert macos_clt.command_line_tools_missing() is missing
    finally:
        macos_clt.command_line_tools_missing.cache_clear()
    # ``xcode-select -p`` never opens the install dialog; nothing else is run.
    assert calls == ([] if platform != "darwin" else [["/usr/bin/xcode-select", "-p"]])


@pytest.mark.skipif(os.name == "nt", reason="POSIX shell wrappers")
def test_a_shell_runs_clios_python_instead_of_the_python3_stub(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, no_command_line_tools: Path
) -> None:
    usr_bin = tmp_path / "usr-bin"
    _stub(usr_bin, "python3", no_command_line_tools)
    clio_python = _tool(tmp_path / "clio", "python3.14")
    monkeypatch.setattr("clio_agent.paths.user_cache_dir", lambda: tmp_path / "cache")
    environment = {"PATH": str(usr_bin), "CLIO_PYTHON": str(clio_python)}

    added = execution._without_command_line_tools_stubs(environment)

    tools = Path(added["PATH"].split(os.pathsep)[0])
    assert added["PATH"].split(os.pathsep)[1:] == [str(usr_bin)]
    assert added["CLIO_EXECUTION_PATH"] == str(tools)
    assert str(clio_python) in (tools / "python3").read_text(encoding="utf-8")
    stand_in = subprocess.run(
        [str(tools / "install_name_tool"), "-id", "x", "y"],
        capture_output=True,
        text=True,
        check=False,
    )
    assert stand_in.returncode == 1
    assert "Command Line Tools" in stand_in.stderr


def test_a_shell_with_a_real_python3_is_left_alone(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, no_command_line_tools: Path
) -> None:
    real = tmp_path / "bin"
    _tool(real, "python3")
    monkeypatch.setattr("clio_agent.paths.user_cache_dir", lambda: tmp_path / "cache")

    assert execution._without_command_line_tools_stubs({"PATH": str(real)}) == {}
    assert not (tmp_path / "cache").exists()
