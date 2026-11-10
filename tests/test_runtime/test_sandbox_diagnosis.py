"""#1905: the sandbox-boundary diagnosis a confined seam attaches to its result.

Unit coverage on every platform, no fence needed: the denial table per platform and shell
(including localized Windows text that matches only on its numeric code), the candidate
paths, the territory check, the folder to ask for, the floor, elevation and the messages.
"""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from clio_agent.runtime import sandbox_diagnosis as sd


@pytest.fixture
def territory(tmp_path: Path) -> tuple[Path, Path]:
    workspace = tmp_path / "ws"
    workspace.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    return workspace, outside


def _posix(stderr: str, workspace: Path, *, command: str = "", exit_code: int | None = 1):
    return sd.diagnose_boundary(
        exit_code=exit_code,
        stderr=stderr,
        command=command,
        cwd=workspace,
        write_roots=(workspace,),
        mechanism="landlock",
        platform="linux" if os.name != "nt" else "win32",
    )


def _windows(stderr: str, *, command: str = "", exit_code: int | None = 1):
    return sd.diagnose_boundary(
        exit_code=exit_code,
        stderr=stderr,
        command=command,
        cwd="C:\\Users\\daisy\\ws",
        write_roots=("C:\\Users\\daisy\\ws", "C:\\Users\\daisy\\AppData\\Local\\Temp"),
        mechanism="codex",
        platform="win32",
    )


_DESKTOP_FILE = "C:\\Users\\daisy\\Desktop\\out.csv"
_DESKTOP = "C:\\Users\\daisy\\Desktop"


@pytest.mark.parametrize(
    ("stderr", "command", "signature"),
    [
        # PowerShell's own wording, with the path extracted from it.
        (
            f"Copy-Item : Access to the path '{_DESKTOP_FILE}' is denied.\n"
            "At line:1 char:1\n"
            f"    + CategoryInfo : PermissionDenied: ({_DESKTOP_FILE}:FileInfo) [Copy-Item], "
            "UnauthorizedAccessException",
            "Copy-Item a.csv C:\\Users\\daisy\\Desktop",
            "ERROR_ACCESS_DENIED",
        ),
        # PowerShell's message on its own (no CategoryInfo line): the path is in the message.
        (
            f"Set-Content : Access to the path '{_DESKTOP_FILE}' is denied.",
            "",
            "ERROR_ACCESS_DENIED",
        ),
        # Localized PowerShell (German): only the exception name matches, and its line
        # names the target in CategoryInfo.
        (
            f'Copy-Item : Der Zugriff auf den Pfad "{_DESKTOP_FILE}" wurde verweigert.\n'
            f"    + CategoryInfo : PermissionDenied: ({_DESKTOP_FILE}:FileInfo) [Copy-Item], "
            "UnauthorizedAccessException",
            "",
            "ERROR_ACCESS_DENIED",
        ),
        # A Python child on Windows, localized: matches on the WinError number alone.
        (
            f"PermissionError: [WinError 5] Zugriff verweigert: '{_DESKTOP_FILE}'",
            "",
            "ERROR_ACCESS_DENIED",
        ),
        # .NET HRESULT, localized, no path in the line: the path comes from the command.
        (
            "Ausnahme von HRESULT: 0x80070005 (E_ACCESSDENIED)",
            f'some.exe --out "{_DESKTOP_FILE}"',
            "ERROR_ACCESS_DENIED",
        ),
        # cmd prints no path at all: the path comes from the command.
        (
            "Access is denied.\n        0 file(s) copied.",
            f"copy a.csv {_DESKTOP_FILE}",
            "ERROR_ACCESS_DENIED",
        ),
    ],
)
def test_windows_denials_name_the_folder_to_ask_for(
    stderr: str, command: str, signature: str
) -> None:
    """SABOTAGE: drop the PowerShell, UnauthorizedAccessException, WinError or 0x80070005
    row from ``DENIAL_SIGNATURES``, or the command fallback, and a case goes red."""
    diagnosis = _windows(stderr, command=command)
    assert diagnosis is not None
    assert diagnosis.kind == sd.KIND_WRITE_DENIED
    assert diagnosis.confidence == sd.CONFIRMED
    assert diagnosis.signature == signature
    assert diagnosis.paths == (_DESKTOP_FILE,)
    assert diagnosis.request_roots == (_DESKTOP,)
    assert f"{_DESKTOP} is not a permitted folder" in diagnosis.message
    assert f"access_paths={json.dumps([_DESKTOP])}" in diagnosis.message
    assert "access_reason" in diagnosis.message and "approve the folder" in diagnosis.message


@pytest.mark.skipif(os.name == "nt", reason="POSIX shell and tool text with POSIX paths")
@pytest.mark.parametrize(
    ("template", "signature"),
    [
        ("/usr/bin/bash: line 1: {p}: Read-only file system", "EROFS"),  # bwrap redirect
        ("/bin/sh: 1: cannot create {p}: Permission denied", "EACCES"),  # Landlock, sh
        ("cp: cannot create regular file '{p}': Permission denied", "EACCES"),
        ("mkdir: cannot create directory ‘{p}’: Permission denied", "EACCES"),  # GNU quotes
        ("touch: {p}: Operation not permitted", "EPERM"),  # macOS Seatbelt
        (
            "Traceback (most recent call last):\n"
            '  File "<string>", line 1, in <module>\n'
            "PermissionError: [Errno 13] Permission denied: '{p}'",
            "EACCES",
        ),
        ("OSError: [Errno 30] Read-only file system: '{p}'", "EROFS"),
        ("Error: EACCES: permission denied, open '{p}'", "EACCES"),  # node
    ],
)
def test_posix_denials_outside_the_territory_are_confirmed(
    territory: tuple[Path, Path], template: str, signature: str
) -> None:
    workspace, outside = territory
    denied = outside / "deep" / "out.txt"
    diagnosis = _posix(template.format(p=denied), workspace)
    assert diagnosis is not None
    assert (diagnosis.confidence, diagnosis.signature) == (sd.CONFIRMED, signature)
    assert diagnosis.paths == (str(denied),)
    # The nearest EXISTING parent: ``deep`` does not exist, so the grant names ``outside``.
    assert diagnosis.request_roots == (str(outside),)


def test_a_denial_inside_the_territory_is_an_ordinary_permission_error(
    territory: tuple[Path, Path],
) -> None:
    """SABOTAGE: skip the ``within_roots`` filter and this is diagnosed -> red."""
    workspace, _ = territory
    assert _posix(f"cat: {workspace}/secret.txt: Permission denied", workspace) is None
    assert _posix("PermissionError: [Errno 13] Permission denied: 'notes.txt'", workspace) is None


def test_a_denial_without_a_path_is_only_possible(territory: tuple[Path, Path]) -> None:
    workspace, _ = territory
    diagnosis = _posix(
        "git@github.com: Permission denied (publickey).", workspace, command="git push"
    )
    assert diagnosis is not None
    assert diagnosis.confidence == sd.POSSIBLE
    assert diagnosis.paths == () and diagnosis.request_roots == ()
    assert diagnosis.message.startswith("This failure may have been caused by CLIO's sandbox")
    assert "couldn't tell which path" in diagnosis.message


def test_the_command_names_the_path_when_the_denial_does_not(
    territory: tuple[Path, Path],
) -> None:
    workspace, outside = territory
    diagnosis = _posix("Permission denied", workspace, command=f"cp a.csv {outside}/b.csv")
    assert diagnosis is not None and diagnosis.paths == (str(outside / "b.csv"),)
    relative = _posix("Permission denied", workspace, command="cp a.csv ./b/c.csv")
    assert relative is None  # resolved against cwd: inside the workspace


def test_no_signature_no_success_and_the_floor_give_nothing(territory: tuple[Path, Path]) -> None:
    """SABOTAGE: drop the ``mechanism == "none"`` test and the floor case goes red."""
    workspace, outside = territory
    denial = f"cp: cannot create regular file '{outside}/x': Permission denied"
    assert _posix("No such file or directory", workspace, command=f"cat {outside}/x") is None
    assert _posix(denial, workspace, exit_code=0) is None
    for mechanism in ("none", ""):
        assert (
            sd.diagnose_boundary(
                exit_code=1,
                stderr=denial,
                command="",
                cwd=workspace,
                write_roots=(workspace,),
                mechanism=mechanism,
            )
            is None
        )


@pytest.mark.parametrize(
    ("exit_code", "stderr"),
    [
        (740, ""),
        (
            1,
            "Start-Process : This command cannot be run due to the error: The requested operation requires elevation.",
        ),
    ],
)
def test_elevation_is_its_own_kind(exit_code: int, stderr: str) -> None:
    """SABOTAGE: drop the 740 test or the "requires elevation" text and a case goes red."""
    diagnosis = _windows(stderr, command="setup.exe /S", exit_code=exit_code)
    assert diagnosis is not None
    assert diagnosis.kind == sd.KIND_ELEVATION_REQUIRED
    assert diagnosis.request_roots == ()
    assert "Tell the person the exact file to run" in diagnosis.message
    assert "Don't try other ways to elevate." in diagnosis.message
    assert "access_paths" not in diagnosis.message


def test_a_folder_the_person_cannot_write_either_is_not_the_fence(
    territory: tuple[Path, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    """A folder the server's own account cannot write (``/etc``) is a file permission, and a
    folder grant would not fix it. SABOTAGE: return True from ``_person_could_write``."""
    if os.name == "nt":
        pytest.skip("Windows os.access ignores ACLs; the check is POSIX-only")
    workspace, outside = territory
    real_access = os.access
    monkeypatch.setattr(
        sd.os, "access", lambda p, mode: False if Path(p) == outside else real_access(p, mode)
    )
    assert _posix(f"cat: '{outside}/shadow': Permission denied", workspace) is None


def test_nearest_existing_parent(tmp_path: Path) -> None:
    (tmp_path / "a").mkdir()
    (tmp_path / "a" / "f.txt").write_text("x")
    assert sd.nearest_existing_parent(str(tmp_path / "a" / "f.txt")) == str(tmp_path / "a")
    assert sd.nearest_existing_parent(str(tmp_path / "a")) == str(tmp_path / "a")
    assert sd.nearest_existing_parent(str(tmp_path / "a" / "x" / "y" / "z.txt")) == str(
        tmp_path / "a"
    )
    # Another platform's path: the immediate parent, lexically.
    assert sd.nearest_existing_parent(_DESKTOP_FILE, platform="win32") == _DESKTOP


def test_windows_territory_is_case_insensitive() -> None:
    inside = "c:\\users\\DAISY\\ws\\out.csv"
    assert sd.within_roots(inside, ("C:\\Users\\daisy\\ws",), platform="win32")
    assert not sd.within_roots(_DESKTOP_FILE, ("C:\\Users\\daisy\\ws",), platform="win32")


def test_a_narrowed_territory_names_where_the_call_may_write(
    territory: tuple[Path, Path],
) -> None:
    """Plan mode / a read-only task: no folder grant widens it, so no retry is offered."""
    workspace, outside = territory
    scratch = outside / "scratch"
    diagnosis = sd.diagnose_boundary(
        exit_code=1,
        stderr=f"PermissionError: [Errno 13] Permission denied: '{workspace}/x.txt'",
        command="",
        cwd=workspace,
        write_roots=(scratch,),
        mechanism="landlock",
        narrowed=True,
    )
    assert diagnosis is not None and diagnosis.request_roots == ()
    assert f"may write only in {scratch}" in diagnosis.message
    assert "access_paths" not in diagnosis.message


def test_a_script_and_a_tool_ask_through_the_shell(territory: tuple[Path, Path]) -> None:
    workspace, outside = territory
    denial = f"PermissionError: [Errno 13] Permission denied: '{outside}/x.txt'"
    script = sd.diagnose_boundary(
        exit_code=None,
        stderr=denial,
        command="",
        cwd=workspace,
        write_roots=(workspace,),
        mechanism="landlock",
        seam="script",
    )
    assert script is not None and "shell_bash call that declares access_paths=" in script.message
    tool = sd.hedged(script)
    assert tool.confidence == sd.POSSIBLE and tool.paths == script.paths
    assert tool.message.startswith("This failure may have been caused by CLIO's sandbox")
    assert f"access_paths={json.dumps([str(outside)])}" in tool.message


def test_the_payload_round_trips_and_is_found_on_a_result(territory: tuple[Path, Path]) -> None:
    workspace, outside = territory
    diagnosis = _posix(
        f"cp: cannot create regular file '{outside}/x': Permission denied", workspace
    )
    assert diagnosis is not None
    payload = diagnosis.to_payload()
    assert set(payload) >= {"kind", "confidence", "mechanism", "paths", "request_roots", "message"}
    assert sd.BoundaryDiagnosis.from_payload(payload) == diagnosis
    assert sd.result_boundary({"exit_code": 1, "boundary": payload}) == diagnosis
    assert sd.result_boundary({"structuredContent": {"boundary": payload}}) == diagnosis
    assert sd.result_boundary({"exit_code": 1}) is None


def test_a_published_boundary_is_taken_once_and_only_by_its_call(
    territory: tuple[Path, Path],
) -> None:
    workspace, outside = territory
    diagnosis = _posix(
        f"cp: cannot create regular file '{outside}/x': Permission denied", workspace
    )
    assert diagnosis is not None
    sd.publish_call_boundary("c1", diagnosis)
    assert sd.take_call_boundary("c2") is None
    assert sd.take_call_boundary("c1") is None  # the slot was cleared by the mismatch
    sd.publish_call_boundary("c1", diagnosis)
    assert sd.take_call_boundary("c1") == diagnosis
    assert sd.take_call_boundary("c1") is None


def test_a_path_quoted_inside_another_quote_is_found(territory: tuple[Path, Path]) -> None:
    """A raised MCP error reaches the boundary as its ``repr``: the path sits in single
    quotes inside double quotes. SABOTAGE: scan all quote kinds with one alternation -> red."""
    workspace, outside = territory
    error = repr(
        RuntimeError(f"Error calling tool 'x': [Errno 13] Permission denied: '{outside}/a'")
    )
    diagnosis = _posix(error, workspace)
    assert diagnosis is not None and diagnosis.paths == (str(outside / "a"),)
