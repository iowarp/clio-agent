"""Native command bounds distinguish launcher limits from CLIO's input budget."""

from __future__ import annotations

import errno
import json
import subprocess
from pathlib import Path

import pytest

from clio_agent.tools.result_room import ResultRoom
from clio_agent.tools.servers.shell_commands import (
    is_command_length_exception,
    is_command_length_output,
    windows_launch_error,
)
from clio_agent.tools.servers.shell_output import StreamCapture, compose_output_fields


@pytest.mark.parametrize("launcher", ["cmd.exe", "cmd", "C:/tools/codex.CMD", "C:/tools/run.bat"])
def test_batch_launcher_rejects_a_command_below_clio_default_budget(launcher: str) -> None:
    command = "Write-Output '" + "x" * 11_000 + "'"
    argv = [launcher, "pwsh.exe", "-NoProfile", "-Command", command]
    error = windows_launch_error(argv, command)
    assert error is not None
    assert error["error"]["code"] == "command_too_long"
    details = error["error"]["details"]
    assert details["max_chars"] == 8191
    assert details["received_chars"] == len(command) < 16_000
    assert "small shell calls" in details["next_action"]
    assert "here-string" in details["next_action"]
    assert windows_launch_error(["C:/tools/pwsh.exe", "-Command", command], command) is None


def test_native_bound_counts_quoting_and_utf16_units_in_wrapper_arguments() -> None:
    command = "🧪" * 16_000
    argv = ["pwsh.exe", "-Command", command, "wrapper-argument-" + "x" * 1000]
    error = windows_launch_error(argv, command)
    assert error is not None
    expected = len(subprocess.list2cmdline(argv).encode("utf-16-le")) // 2
    assert error["error"]["details"]["launch_chars"] == expected > 32766
    assert error["error"]["details"]["received_chars"] == 16_000


def test_quotes_can_exceed_the_launch_limit_even_when_the_program_text_fits() -> None:
    command = '"' * 16_000
    argv = ["pwsh.exe", "-Command", command, "x" * 1000]
    error = windows_launch_error(argv, command)
    assert error is not None
    assert error["error"]["details"]["launch_chars"] > 32766


def test_native_length_exceptions_do_not_depend_on_the_message_language() -> None:
    windows_error = OSError("localized message")
    windows_error.winerror = 206
    assert is_command_length_exception(windows_error)
    assert is_command_length_exception(OSError(errno.E2BIG, "localized message"))
    assert not is_command_length_exception(PermissionError("access denied"))


def test_native_diagnostic_match_is_narrow() -> None:
    assert is_command_length_output("\r\nThe command line is too long.\r\n")
    assert not is_command_length_output("The article discusses 'the command line is too long'.")


@pytest.mark.parametrize("exit_code", [0, 1, None])
def test_completed_output_retains_diagnostics_and_bounds_recovery_metadata(
    tmp_path: Path, exit_code: int | None
) -> None:
    captures = [
        StreamCapture(name, inline_limit=8192, spill_path=tmp_path / f"{name}.txt")
        for name in ("stdout", "stderr")
    ]
    captures[1].feed(b"The command line is too long.\r\n")
    for capture in captures:
        capture.finish()
    room = ResultRoom(tokens=1_750, chars=7_000, reason="pinned")
    result = compose_output_fields(
        {"command": "x" * 11_671, "exit_code": exit_code}, captures, root=tmp_path, budget=room
    )
    if exit_code == 1:
        assert result["error"]["code"] == "command_too_long"
        assert result["error"]["details"]["bound"] == "native_shell"
        assert result["error"]["details"]["received_chars"] == 11_671
        assert "command_omitted" in result
    else:
        assert "error" not in result
    assert "The command line is too long." in result["stderr"] or (
        "The command line is too long." in Path(result["stderr_spill"]["path"]).read_text()
    )
    assert room.fits(json.dumps(result))
