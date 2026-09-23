"""Tests for CLIO's local shell utility tool."""

from __future__ import annotations

import asyncio
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


def test_shell_bash_declares_open_world_destructive_annotations() -> None:
    """#1061: shell_bash declares the most-restrictive open-world destructive annotations,
    which PROJECT to NO catalog read/write tag — effectful/unclassifiable, so it is never
    read-only and never an fs-write the auto-edits mode auto-approves (OS fence owns its
    effects)."""
    from clio_agent.tools.catalog import classification_tags, get_tool_entry
    from clio_agent.tools.gateway import _list_tools_sync, _tool_annotations

    listed = {t.name: t for t in _list_tools_sync(shell_server)}
    annotations = _tool_annotations(listed["bash"])
    assert annotations is not None
    assert annotations["readOnlyHint"] is False
    assert annotations["destructiveHint"] is True
    assert annotations["openWorldHint"] is True
    # open-world effectful -> NEITHER read nor write.
    assert classification_tags(annotations) == frozenset()
    tags = get_tool_entry("shell_bash").tags
    assert "read" not in tags
    assert "write" not in tags


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
async def test_shell_bash_streams_typed_terminal_chunks_before_completion(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Shell stdout reaches MCP progress before the process exits."""

    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("CLIO_ALLOWED_ROOTS", str(tmp_path))
    conf.reload()
    script = tmp_path / "stream_probe.py"
    script.write_text(
        "import time\nprint('FIRST', flush=True)\ntime.sleep(0.5)\nprint('SECOND', flush=True)\n",
        encoding="utf-8",
    )
    if os.name == "nt":
        command = f"& '{sys.executable}' -u '{script}'"
    else:
        command = f"'{sys.executable}' -u '{script}'"
    progress_messages: list[str] = []
    first_chunk = asyncio.Event()

    async def on_progress(
        _progress: float,
        _total: float | None,
        message: str | None,
    ) -> None:
        if message is not None:
            progress_messages.append(message)
            first_chunk.set()

    try:
        async with Client(shell_server) as client:
            call = asyncio.create_task(
                client.call_tool(
                    "bash",
                    {"command": command, "cwd": str(tmp_path), "timeout_s": 5},
                    progress_handler=on_progress,
                )
            )
            await asyncio.wait_for(first_chunk.wait(), timeout=2.0)
            assert not call.done(), "first output was buffered until process completion"
            result = await call
    finally:
        conf.reload()

    chunks = [json.loads(message) for message in progress_messages]
    assert all(chunk["type"] == "clio.terminal.chunk" for chunk in chunks)
    assert "FIRST" in "".join(chunk["text"] for chunk in chunks)
    data = _parse_result(result)
    assert data["stdout"] == "FIRST\nSECOND\n"


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


def _spawn_tree_command(pid_file: Path, parent_sleep_s: int) -> str:
    """A command whose shell starts a grandchild python that records its pid and sleeps."""

    workdir = pid_file.parent
    child = workdir / "tree_child.py"
    child.write_text(
        "import os, sys, time\n"
        "open(sys.argv[1], 'w').write(str(os.getpid()))\n"
        "time.sleep(120)\n",
        encoding="utf-8",
    )
    parent = workdir / "tree_parent.py"
    parent.write_text(
        "import subprocess, sys, time\n"
        "subprocess.Popen([sys.executable, sys.argv[1], sys.argv[2]])\n"
        f"time.sleep({parent_sleep_s})\n",
        encoding="utf-8",
    )
    if os.name == "nt":
        return f"& '{sys.executable}' '{parent}' '{child}' '{pid_file}'"
    return f"'{sys.executable}' '{parent}' '{child}' '{pid_file}'"


def _wait_for_pid(pid_file: Path, timeout_s: float = 20.0) -> int:
    import time

    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        if pid_file.exists() and pid_file.read_text().strip():
            return int(pid_file.read_text())
        time.sleep(0.1)
    raise AssertionError("grandchild never recorded its pid")


def _pid_gone(pid: int, timeout_s: float = 10.0) -> bool:
    import time

    import psutil

    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        try:
            if psutil.Process(pid).status() == psutil.STATUS_ZOMBIE:
                return True
        except psutil.NoSuchProcess:
            return True
        time.sleep(0.1)
    return False


def _shell_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("CLIO_ALLOWED_ROOTS", str(tmp_path))
    conf.reload()


def _sleep_command(seconds: float) -> str:
    code = f"import time; time.sleep({seconds}); print('DONE')"
    if os.name == "nt":
        return f"& '{sys.executable}' -c \"{code}\""
    return f"'{sys.executable}' -c \"{code}\""


@pytest.mark.asyncio
async def test_shell_bash_has_no_timeout_by_default(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A command with no timeout_s runs to completion; the old 5s default killed it."""

    _shell_env(tmp_path, monkeypatch)
    try:
        async with Client(shell_server) as client:
            result = await client.call_tool(
                "bash", {"command": _sleep_command(6.5), "cwd": str(tmp_path)}
            )
    finally:
        conf.reload()
    data = _parse_result(result)
    assert data["timed_out"] is False
    assert data["exit_code"] == 0
    assert data["stdout"].strip() == "DONE"


@pytest.mark.asyncio
async def test_shell_bash_explicit_timeout_kills_the_whole_tree(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A model-requested timeout is honored and reaps grandchildren, not just the shell."""

    _shell_env(tmp_path, monkeypatch)
    pid_file = tmp_path / "grandchild.pid"
    try:
        async with Client(shell_server) as client:
            result = await client.call_tool(
                "bash",
                {
                    "command": _spawn_tree_command(pid_file, parent_sleep_s=120),
                    "cwd": str(tmp_path),
                    "timeout_s": 8,
                },
            )
    finally:
        conf.reload()
    data = _parse_result(result)
    assert data["timed_out"] is True
    assert _pid_gone(_wait_for_pid(pid_file))


@pytest.mark.asyncio
async def test_shell_bash_cancellation_kills_the_whole_tree(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """With no timeout, a cancelled turn must still stop the command and its children."""

    _shell_env(tmp_path, monkeypatch)
    pid_file = tmp_path / "grandchild.pid"
    try:
        async with Client(shell_server) as client:
            call = asyncio.create_task(
                client.call_tool(
                    "bash",
                    {
                        "command": _spawn_tree_command(pid_file, parent_sleep_s=120),
                        "cwd": str(tmp_path),
                    },
                )
            )
            grandchild = await asyncio.to_thread(_wait_for_pid, pid_file)
            call.cancel()
            with pytest.raises(asyncio.CancelledError):
                await call
            assert await asyncio.to_thread(_pid_gone, grandchild)
    finally:
        conf.reload()


@pytest.mark.asyncio
async def test_shell_bash_rejects_negative_timeout(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _shell_env(tmp_path, monkeypatch)
    try:
        async with Client(shell_server) as client:
            result = await client.call_tool(
                "bash",
                {"command": _sleep_command(0), "cwd": str(tmp_path), "timeout_s": -1},
            )
    finally:
        conf.reload()
    data = _parse_result(result)
    assert data["error"]["code"] == "invalid_timeout"


@pytest.mark.asyncio
async def test_shell_bash_operator_ceiling_bounds_unspecified_and_larger_timeouts(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An operator ceiling caps a call without timeout_s and refuses a larger request."""

    import importlib

    module = importlib.import_module("clio_agent.tools.servers.shell_server")
    monkeypatch.setattr(module, "_MAX_TIMEOUT_S", 3.0)
    _shell_env(tmp_path, monkeypatch)
    try:
        async with Client(shell_server) as client:
            capped = _parse_result(
                await client.call_tool(
                    "bash", {"command": _sleep_command(30), "cwd": str(tmp_path)}
                )
            )
            refused = _parse_result(
                await client.call_tool(
                    "bash",
                    {"command": _sleep_command(0), "cwd": str(tmp_path), "timeout_s": 10},
                )
            )
    finally:
        conf.reload()
    assert capped["timed_out"] is True
    assert capped["timeout_s"] == 3.0
    assert refused["error"]["code"] == "invalid_timeout"
