"""Tests for CLIO's local shell utility tool."""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

import pytest
from fastmcp import Client

from clio_agent import conf
from clio_agent.tools.servers.shell_server import (
    ShellEnvFacts,
    _detect_shell_env,
    _translate_windows_paths_for_bash,
    build_shell_tool_description,
    shell_server,
)


def test_shell_description_windows_warns_off_wsl_and_steers_pandas():
    """On Windows the tool description pins PowerShell, no-WSL, and pandas steering (#898)."""
    facts = ShellEnvFacts(
        is_windows=True,
        system_label="Windows",
        shell_label="PowerShell 5.1",
        posix_text_tools=False,
    )
    text = build_shell_tool_description(facts)
    assert "Windows" in text
    assert "PowerShell 5.1" in text
    assert "Do NOT assume WSL exists" in text
    assert "wsl" in text.lower()
    assert "pandas" in text
    assert "NOT available" in text  # POSIX text tools absent


def test_shell_description_posix_has_no_wsl_and_steers_pandas():
    """On POSIX the description carries no WSL warning but keeps pandas steering (#898)."""
    facts = ShellEnvFacts(
        is_windows=False,
        system_label="Linux",
        shell_label="bash 5.2",
        posix_text_tools=True,
    )
    text = build_shell_tool_description(facts)
    assert "Linux" in text
    assert "wsl" not in text.lower()
    assert "POSIX conventions" in text
    assert "pandas" in text
    assert "ARE available" in text  # POSIX text tools present


def test_detect_shell_env_platform_is_monkeypatchable(monkeypatch):
    """Platform detection reads os.name so tests pin the Windows/POSIX branch (#898).

    The shell module uses the shared ``os``/``shutil`` module objects, so patching
    them here forces the detection branch without running on that OS. Detection
    spawns no subprocess (#898), so no version-probe stub is needed.
    """
    import shutil

    monkeypatch.setattr(shutil, "which", lambda _tool: None)
    monkeypatch.setattr(os, "name", "nt")
    win = _detect_shell_env()
    assert win.is_windows is True
    assert win.posix_text_tools is False

    monkeypatch.setattr(os, "name", "posix")
    monkeypatch.setattr(shutil, "which", lambda _tool: "/usr/bin/" + _tool)
    posix = _detect_shell_env()
    assert posix.is_windows is False
    assert posix.posix_text_tools is True


def _parse_result(result: object) -> dict:
    data = getattr(result, "data", result)
    if isinstance(data, dict):
        return data
    if isinstance(data, str):
        return json.loads(data)
    raise AssertionError(f"unexpected result type: {type(data)!r}")


@pytest.mark.asyncio
async def test_shell_bash_runs_simple_command(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The shell tool should execute a bounded command and return output."""

    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("CLIO_ALLOWED_ROOTS", str(tmp_path))
    conf.reload()
    if os.name == "nt":
        command = f"& '{sys.executable}' -c \"print('CLIO_SHELL_OK')\""
    else:
        command = f"'{sys.executable}' -c \"print('CLIO_SHELL_OK')\""

    try:
        async with Client(shell_server) as client:
            result = await client.call_tool(
                "bash",
                {"command": command, "cwd": str(tmp_path), "timeout_s": 5},
            )
    finally:
        conf.reload()

    data = _parse_result(result)
    assert data["exit_code"] == 0
    assert data["timed_out"] is False
    assert data["stdout"].strip() == "CLIO_SHELL_OK"
    assert data["stderr"] == ""


@pytest.mark.asyncio
async def test_shell_bash_rejects_cwd_outside_allowed_roots(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The shell working directory must obey the file policy."""

    allowed = tmp_path / "allowed"
    outside = tmp_path / "outside"
    allowed.mkdir()
    outside.mkdir()
    # allowed_roots lives in the config FILE (file > env); write the NARROWER root
    # there, overwriting the fixture's ``tmp_path`` list so ``outside`` is rejected
    # (a bare setenv would be shadowed by the fixture file — #985 residual).
    from tests._config_layer import set_config

    set_config("tools.file_policy.allowed_roots", [str(allowed)])

    async with Client(shell_server) as client:
        result = await client.call_tool(
            "bash",
            {"command": "echo should-not-run", "cwd": str(outside)},
        )

    data = _parse_result(result)
    assert data["error"]["type"] == "file_policy"
    assert data["error"]["code"] == "outside_allowed_roots"


def test_translate_windows_paths_for_bash() -> None:
    """WSL bash receives mounted paths when a model emits Windows paths."""

    command = r'head -5 "D:\Libraries\Documents\projects\data.csv" > D:\tmp\out.csv'

    translated = _translate_windows_paths_for_bash(command)

    assert (
        translated == 'head -5 "/mnt/d/Libraries/Documents/projects/data.csv" > /mnt/d/tmp/out.csv'
    )
