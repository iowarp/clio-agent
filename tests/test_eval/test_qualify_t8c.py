"""T8c counts each script's own write questions. On the 2026-11-04 rerun the agent
checked its files with a shell call after each script; under approval mode ask that
call asked too, and the check counted it as the script's second write question."""

from __future__ import annotations

from typing import Any

from scripts.eval.tool_calling.qualify import check_t8c


def _row(tool: str, answered: str = "allow") -> dict[str, Any]:
    return {"tool_call": {"tool_name": tool}, "answered": answered}


def test_a_shell_check_after_the_script_is_not_the_scripts_question() -> None:
    """SABOTAGE: count every question after a run_code row and this reads fail."""
    permissions = [
        _row("run_code"), _row("fs_write_file", "allow_script"), _row("shell_bash"),
        _row("run_code"), _row("fs_write_file", "allow_script"), _row("shell_bash"),
    ]  # fmt: skip
    files = ["out/1.txt", "out/2.txt", "out/3.txt", "out/4.txt"]
    status, notes = check_t8c({"permissions": permissions, "files": files})
    assert status == "pass", notes


def test_a_second_write_question_in_one_script_fails() -> None:
    permissions = [
        _row("run_code"), _row("fs_write_file", "allow_script"), _row("fs_write_file"),
        _row("run_code"), _row("fs_write_file", "allow_script"),
    ]  # fmt: skip
    files = ["out/1.txt", "out/2.txt", "out/3.txt", "out/4.txt"]
    assert check_t8c({"permissions": permissions, "files": files})[0] == "fail"
