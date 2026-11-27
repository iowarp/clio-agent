"""The shell read classifier treats plain listing and reading commands as reads, and nothing else."""

from __future__ import annotations

import importlib

import pytest

from clio_agent.gact.runtime import grant_resolver
from clio_agent.tools import shell_read_only
from clio_agent.tools.shell_read_only import command_is_read_only, shell_call_is_read_only


@pytest.mark.parametrize(
    "command",
    [
        "Get-ChildItem",
        "Get-ChildItem -Recurse -Filter *.py C:\\data",
        "gci 'C:\\Program Files' | Select-Object Name, Length",
        "dir; ls -Name",
        'Get-Content "notes — 20°C.txt" -TotalCount 20',
        "cat README.md | Select-String -Pattern 'TODO' -SimpleMatch",
        "Test-Path .\\run.ps1",
        "Get-ChildItem -Force",
        "Get-ChildItem -Recurse 2>$null | Measure-Object",
        "Get-Item x.txt 2>&1 | Format-List *",
        "get-content a.txt | where-object Length -gt 0",
        "Get-ChildItem *>$null",
        "Get-FileHash a.bin; Resolve-Path ..",
    ],
)
def test_powershell_reads(command: str) -> None:
    assert command_is_read_only(command, "powershell")


@pytest.mark.parametrize(
    "command",
    [
        "Get-ChildItem > listing.txt",
        "Get-ChildItem 2> err.txt",
        "Get-ChildItem 2>nul.txt",
        "Get-Content a.txt | Set-Content b.txt",
        "Get-ChildItem | Remove-Item -Force",
        "Get-ChildItem; Remove-Item x",
        "Remove-Item -Recurse -Force build",
        "Get-ChildItem | Where-Object { $_.Length -gt 0 }",
        "Get-ChildItem | ForEach-Object { Remove-Item $_ }",
        "Get-Content $(Remove-Item x)",
        'Get-Content "$(Remove-Item x)"',
        "& Remove-Item x",
        "Get-ChildItem | Out-File x.txt",
        "Get-ChildItem | Tee-Object x.txt",
        "$x = Get-ChildItem",
        "C:\\tools\\ls.exe",
        "Get-Content a.txt `\n; Remove-Item x",
        "Get-ChildItem 'unterminated",
        "Get-ChildItem |",
        "",
        "   ",
        "Invoke-Expression 'Remove-Item x'",
        "Start-Process cmd",
        "Get-Content (Get-Item x)",
    ],
)
def test_powershell_non_reads(command: str) -> None:
    assert not command_is_read_only(command, "powershell")


@pytest.mark.parametrize(
    "command",
    [
        "ls -la",
        "cat a.txt | grep -n TODO | head -5",
        "ls src && wc -l README.md",
        "grep -r 'a > b' . 2>/dev/null",
        "stat a; du -sh .",
    ],
)
def test_posix_reads(command: str) -> None:
    assert command_is_read_only(command, "posix")


@pytest.mark.parametrize(
    "command",
    [
        "ls > out",
        "cat a | tee b",
        "ls; rm -rf x",
        "ls & rm x",
        "cat $(rm x)",
        'cat "`rm x`"',
        "echo `rm x`",
        "X=1 ls",
        "find . -delete",
        "sort -o a a",
        "ls \\; rm x",
        "LS",
    ],
)
def test_posix_non_reads(command: str) -> None:
    assert not command_is_read_only(command, "posix")


def test_cmd_dialect() -> None:
    assert command_is_read_only("dir /b C:\\data", "cmd")
    assert command_is_read_only('type "a b.txt" | findstr x', "cmd")
    assert not command_is_read_only("dir > out.txt", "cmd")
    assert not command_is_read_only("dir & del x", "cmd")
    assert not command_is_read_only("dir ^& del x", "cmd")
    assert not command_is_read_only("del x", "cmd")


def test_unknown_dialect_and_long_commands_are_not_reads() -> None:
    assert not command_is_read_only("ls", "fish")
    assert not command_is_read_only("ls " + "a" * 5000, "posix")


def test_shell_call_requires_a_fenced_string_command(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(shell_read_only, "shell_dialect", lambda: "powershell")
    assert shell_call_is_read_only({"command": "Get-ChildItem"})
    assert not shell_call_is_read_only({"command": "Get-ChildItem", "outside_sandbox": True})
    assert not shell_call_is_read_only({"command": ["Get-ChildItem"]})
    assert not shell_call_is_read_only(None)


def test_dialect_follows_the_shell_tool(monkeypatch: pytest.MonkeyPatch) -> None:
    shell_server = importlib.import_module("clio_agent.tools.servers.shell_server")
    monkeypatch.setattr(shell_read_only.os, "name", "nt")
    monkeypatch.setattr(shell_read_only.shutil, "which", lambda name: f"C:\\bin\\{name}")
    for backend, dialect in (("powershell", "powershell"), ("bash", "posix"), ("cmd", "cmd")):
        monkeypatch.setattr(shell_server, "_windows_shell_backend", lambda b=backend: b)
        assert shell_read_only.shell_dialect() == dialect
    monkeypatch.setattr(shell_read_only.shutil, "which", lambda name: None)
    monkeypatch.setattr(shell_server, "_windows_shell_backend", lambda: "powershell")
    assert shell_read_only.shell_dialect() == "cmd"


def test_gate_read_predicate_uses_the_classifier(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(shell_read_only, "shell_dialect", lambda: "powershell")
    read = {"command": "Get-ChildItem -Recurse | Select-Object FullName"}
    write = {"command": "Get-ChildItem | Remove-Item"}
    for name in ("shell_bash", "bash"):
        assert grant_resolver.is_read_only("tool", name, read, None)
        assert not grant_resolver.is_read_only("tool", name, write, None)
    assert not grant_resolver.is_read_only("tool", "shell_bash", None, None)
    assert not grant_resolver.is_read_only("tool", "run_code", read, None)


def test_durable_submission_keeps_a_shell_read_out_of_the_plan_lock(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from clio_agent.gact.task_runtime import authorization_mode

    monkeypatch.setattr(shell_read_only, "shell_dialect", lambda: "powershell")
    read = {"command": "Get-ChildItem -Name"}
    write = {"command": "Remove-Item notes.txt"}
    assert authorization_mode("shell_bash", read, session_mode="plan", human_action=False) == ""
    assert authorization_mode("shell_bash", write, session_mode="plan", human_action=False) == (
        "plan"
    )
    assert authorization_mode("shell_bash", write, session_mode="plan", human_action=True) == ""
    assert grant_resolver.resolve("tool", "shell_bash", policies=[], mode="plan") == "deny"
    assert grant_resolver.resolve("tool", "shell_bash", policies=[], mode="") != "deny"
    person_deny = [
        {"scope": "session", "scope_id": "s", "tool_name_pattern": "shell_bash", "action": "deny"}
    ]
    assert (
        grant_resolver.resolve("tool", "shell_bash", policies=person_deny, session_id="s", mode="")
        == "deny"
    )


@pytest.mark.parametrize(
    "command",
    [
        "& $env:CLIO_PYTHON 'D:\\work\\prepare_wing_cfd.py'",
        "& $env:CLIO_UV run --no-project --python $env:CLIO_PYTHON --with cadquery "
        "--with trimesh 'D:\\work\\naca0012_wing.py'; exit $LASTEXITCODE",
        "$r = gh api repos/o/r/commits | ConvertFrom-Json; $r[0].sha",
        "python -c \"print('a > b')\"",
        "uv run --with numpy solve.py 2>&1",
        "New-Item -ItemType Directory -Force out",
        "Copy-Item a.step b.step",
        "Get-Command uv",
    ],
)
def test_running_a_program_is_a_shell_command_not_destructive(command: str) -> None:
    assert not shell_read_only.command_is_destructive(command)
    assert shell_read_only.shell_call_subject({"command": command}) == "shell command"


@pytest.mark.parametrize(
    "command",
    [
        "Remove-Item -Recurse out",
        "ri out -Force; python run.py",
        "Get-ChildItem *.tmp | Remove-Item",
        "gci | ForEach-Object { del $_ }",
        "rm -rf build",
        "/bin/rm a.txt",
        "python mesh.py > mesh.log",
        "Set-Content a.txt 'x'",
        "'x' | Out-File a.txt",
        "Move-Item a b",
        "git reset --hard HEAD~1",
        "git clean -fdx",
        "find . -name '*.o' -delete",
        "sed -i 's/a/b/' f.txt",
        "[IO.File]::WriteAllText('a.txt', 'x')",
        "gh api -X DELETE repos/o/r/git/refs/heads/x",
        'pwsh -Command "Remove-Item x"',
        "Stop-Process -Id 4242",
    ],
)
def test_deleting_overwriting_or_killing_is_destructive(command: str) -> None:
    assert shell_read_only.command_is_destructive(command)
    assert shell_read_only.shell_call_subject({"command": command}) == "destructive tool"


def test_an_unreadable_or_unsandboxed_shell_call_stays_destructive() -> None:
    subject = shell_read_only.shell_call_subject
    assert subject(None) == "destructive tool"
    assert subject({}) == "destructive tool"
    assert subject({"command": "  "}) == "destructive tool"
    assert subject({"command": "python run.py", "outside_sandbox": True}) == "destructive tool"
    assert subject({"command": "x" * 5000}) == "destructive tool"


def test_the_gate_noun_names_a_shell_call_by_its_command() -> None:
    from clio_agent.tools.catalog import action_subject

    assert action_subject("shell_bash", {"command": "python run.py"}) == "shell command"
    assert action_subject("shell_bash", {"command": "rm -rf out"}) == "destructive tool"
    assert action_subject("shell_bash") == "destructive tool"
    assert action_subject("fs_write_file", {"path": "a"}) == "destructive tool"
