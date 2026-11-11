"""#2139: CLIO's LibreOffice worker on macOS runs under its own Seatbelt policy.

Codex's macOS profile cannot allow a mach service, and AppKit LibreOffice aborts without
the window server and LaunchServices. Pinned here: only the ``office`` spawn on darwin with
the Codex fence leaves Codex (Linux, Windows and every other spawn are unchanged), the
policy adds exactly the two named services to Codex's base, protected roots win over the
territory, and a model's shell ``soffice`` abort is steered to ``prepare_document``.
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from clio_agent.runtime import document_runtime, sandbox, storage_access
from clio_agent.runtime import sandbox_office_seatbelt as office
from clio_agent.runtime.sandbox_diagnosis import KIND_OFFICE_IN_SHELL, diagnose_boundary

_CODEX = sandbox.SandboxResult(
    mechanism=sandbox.MECHANISM_CODEX,
    active=True,
    reason=sandbox.REASON_FENCE_ACTIVE,
    details={"codex_binary": "codex"},
)


@pytest.fixture
def roots(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> dict[str, Path]:
    paths = {name: tmp_path / name for name in ("work", "sources", "secrets", "outside")}
    for path in paths.values():
        path.mkdir()
    monkeypatch.setattr(
        storage_access,
        "storage_access_roots",
        lambda: ((paths["sources"].resolve(),), (paths["secrets"].resolve(),)),
    )
    monkeypatch.setenv("CODEX_HOME", str(tmp_path / "codex-home"))
    return paths


def _compose(platform: str, *, office_spawn: bool, work: Path, monkeypatch: Any) -> tuple:
    monkeypatch.setattr(sys, "platform", platform)
    return sandbox._compose_fence_prefix(
        _CODEX, sandbox.PROFILE_SHELL, "soffice", ["--headless"], [str(work)], office=office_spawn
    )


def test_darwin_office_spawn_uses_the_office_policy(roots, monkeypatch) -> None:
    command, args = _compose(
        "darwin", office_spawn=True, work=roots["work"], monkeypatch=monkeypatch
    )
    assert command == office.SANDBOX_EXEC
    assert args[0] == "-p"
    assert args[-2:] == ["soffice", "--headless"]
    params = [arg for arg in args if arg.startswith("-D")]
    assert f"-DW0={roots['work']}" in params
    assert f"-DR0={roots['sources'].resolve()}" in params
    assert f"-DD0={roots['secrets'].resolve()}" in params
    policy = args[1]
    assert policy.startswith("(version 1)") and "\n(deny default)\n" in policy
    lookups = [
        line for line in policy.splitlines() if "mach-lookup" in line and "global-name" in line
    ]
    assert lookups[-1] == (
        '(allow mach-lookup (global-name "com.apple.windowserver.active") '
        '(global-name "com.apple.coreservices.launchservicesd"))'
    )
    # Last match wins in Seatbelt: the protected roots follow the territory grant.
    assert (
        policy.index('(allow file-write* (subpath (param "W0")))')
        < policy.index('(deny file-write* (subpath (param "R0")))')
        < policy.index('(deny file-read* file-write* (subpath (param "D0")))')
    )
    assert "network-outbound (remote ip" not in policy


@pytest.mark.parametrize(("platform", "office_spawn"), [("linux", True), ("darwin", False)])
def test_every_other_spawn_keeps_codex(roots, monkeypatch, platform, office_spawn) -> None:
    command, args = _compose(
        platform, office_spawn=office_spawn, work=roots["work"], monkeypatch=monkeypatch
    )
    assert command == "codex"
    assert args[0] == "sandbox"


@pytest.mark.skipif(
    sys.platform != "darwin" or not Path(office.SANDBOX_EXEC).exists(), reason="macOS Seatbelt"
)
def test_office_policy_fences_writes_on_macos(roots) -> None:
    command, args = office.office_seatbelt_prefix(
        [str(roots["work"])],
        "/bin/sh",
        [
            "-c",
            'echo ok > "$1/in" && { echo no > "$2/out" || echo fenced; }'
            ' && { echo no > "$3/out" || echo protected; } && { cat "$4/key" || echo denied; }',
            "sh",
            str(roots["work"]),
            str(roots["outside"]),
            str(roots["sources"]),
            str(roots["secrets"]),
        ],
    )
    (roots["secrets"] / "key").write_text("secret")
    done = subprocess.run([command, *args], capture_output=True, text=True, timeout=30)
    assert done.returncode == 0, done.stderr
    assert done.stdout.split() == ["fenced", "protected", "denied"]
    assert (roots["work"] / "in").read_text() == "ok\n"
    assert not (roots["outside"] / "out").exists()


@pytest.mark.parametrize(
    ("args", "expected"),
    [
        (["render", "a.docx"], True),
        (["recalculate", "a.xlsx"], True),
        (["render", "a.pdf"], False),
        (["inspect", "a.docx"], False),
    ],
)
def test_document_helper_marks_only_office_steps(tmp_path, monkeypatch, args, expected) -> None:
    seen: dict[str, Any] = {}

    def wrap(command: str, arguments: list[str], **kwargs: Any) -> SimpleNamespace:
        seen.update(kwargs)
        return SimpleNamespace(
            command=command, args=arguments, env_overlay={}, release=lambda: None
        )

    monkeypatch.setattr(sandbox, "wrap_confined", wrap)
    monkeypatch.setattr(document_runtime, "prepare_office_runtime", lambda: "soffice")
    monkeypatch.setattr(document_runtime, "_run", lambda command, **kwargs: json.dumps({"ok": 1}))
    document_runtime.run_document_helper({"helper_argv": ["python", "cli.py"]}, args, cwd=tmp_path)
    assert seen["office"] is expected


def test_document_helper_without_output_raises_typed(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(
        sandbox,
        "wrap_confined",
        lambda command, args, **kwargs: SimpleNamespace(
            command=command, args=args, env_overlay={}, release=lambda: None
        ),
    )
    monkeypatch.setattr(document_runtime, "_run", lambda command, **kwargs: "")
    with pytest.raises(document_runtime.DocumentRuntimeError, match="without a result"):
        document_runtime.run_document_helper(
            {"helper_argv": ["python", "cli.py"]}, ["inspect", "a.docx"], cwd=tmp_path
        )


@pytest.mark.parametrize(
    ("platform", "exit_code", "command", "kind"),
    [
        ("darwin", 134, "soffice --headless --convert-to pdf a.docx", KIND_OFFICE_IN_SHELL),
        (
            "darwin",
            -6,
            "/Applications/LibreOffice.app/Contents/MacOS/soffice -h",
            KIND_OFFICE_IN_SHELL,
        ),
        ("darwin", 134, "python crash.py", None),
        ("darwin", 1, "soffice --headless --convert-to pdf a.docx", None),
        ("linux", 134, "soffice --headless --convert-to pdf a.docx", None),
    ],
)
def test_shell_soffice_abort_steers_to_prepare_document(platform, exit_code, command, kind) -> None:
    diagnosis = diagnose_boundary(
        exit_code=exit_code,
        stderr="",
        command=command,
        cwd="/tmp",
        write_roots=["/tmp"],
        mechanism=sandbox.MECHANISM_CODEX,
        platform=platform,
    )
    assert (diagnosis.kind if diagnosis else None) == kind
    if diagnosis is not None:
        assert "prepare_document" in diagnosis.message
        assert "action='render'" in diagnosis.message
