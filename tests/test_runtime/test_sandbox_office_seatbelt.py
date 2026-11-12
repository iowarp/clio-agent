"""#2139: CLIO's LibreOffice worker on macOS runs under its own Seatbelt policy.

Codex's macOS profile cannot allow a mach service, and AppKit LibreOffice aborts without
the window server and LaunchServices. Pinned here: only the ``office`` spawn on darwin with
the Codex fence leaves Codex (Linux, Windows and every other spawn are unchanged), the
policy adds exactly the two named services to Codex's base, protected roots win over the
territory, and a model's shell ``soffice`` abort is steered to ``prepare_document``.

LibreOffice's single-instance pipe gets a private directory short enough for ``sun_path``:
without one, a fenced ``soffice`` exits 0 at once without converting anything.
"""

from __future__ import annotations

import json
import os
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


def _short_parent(tmp_path_factory: pytest.TempPathFactory) -> Path:
    # macOS's per-user temp root exceeds sun_path; production uses /private/tmp there.
    base = tmp_path_factory.getbasetemp()
    return (
        base
        if office.pipe_dir_fits(str(base / "clio-lo-abcdefgh"))
        else Path(office.PIPE_DIR_PARENT)
    )


@pytest.fixture
def pipe_parent(monkeypatch, tmp_path_factory) -> Path:
    parent = _short_parent(tmp_path_factory)
    monkeypatch.setattr(office, "PIPE_DIR_PARENT", str(parent))
    return parent


def test_pipe_dir_is_private_short_and_grants_soffice_its_pipe(roots, pipe_parent) -> None:
    pipe_dir = office.create_office_pipe_dir()
    try:
        assert Path(pipe_dir).parent == pipe_parent
        assert Path(pipe_dir).stat().st_mode & 0o777 == 0o700
        # Directory, separator, pipe name and NUL fit macOS sun_path.
        name = f"OSL_PIPE_{os.getuid()}_SingleOfficeIPC_{'0' * 32}"
        assert len(os.fsencode(f"{pipe_dir}/{name}")) + 1 <= office.SUN_PATH_MAX
        command, args = office.office_seatbelt_prefix(
            [str(roots["work"])],
            "/Apps/LibreOffice.app/Contents/MacOS/soffice",
            ["-env:OSL_SOCKET_PATH=/stale", "--headless", "--convert-to", "pdf"],
            pipe_dir=pipe_dir,
        )
        assert command == office.SANDBOX_EXEC
        params = [arg for arg in args if arg.startswith("-DW")]
        assert params[0] == f"-DW0={roots['work']}"
        assert f"={pipe_dir}" in params[-1]
        assert '(allow file-write* (subpath (param "W0")) (subpath (param "W1"))' in args[1]
        launch = args[args.index("/Apps/LibreOffice.app/Contents/MacOS/soffice") :]
        assert launch[1:] == [
            f"-env:OSL_SOCKET_PATH={pipe_dir}",
            "--headless",
            "--convert-to",
            "pdf",
        ]
    finally:
        office.remove_office_pipe_dir(pipe_dir)
    assert not Path(pipe_dir).exists()


def test_helper_launch_keeps_its_argv_and_reads_the_pipe_from_env(roots, pipe_parent) -> None:
    pipe_dir = office.create_office_pipe_dir()
    try:
        _, args = office.office_seatbelt_prefix(
            [str(roots["work"])], "python", ["cli.py", "render", "a.docx"], pipe_dir=pipe_dir
        )
        assert args[-4:] == ["python", "cli.py", "render", "a.docx"]
        assert f"={pipe_dir}" in [arg for arg in args if arg.startswith("-DW")][-1]
    finally:
        office.remove_office_pipe_dir(pipe_dir)


def test_pipe_dir_bound_rejects_long_paths() -> None:
    assert office.office_pipe_name_length(501) == len("OSL_PIPE_501_SingleOfficeIPC_") + 32
    assert office.pipe_dir_fits("/private/tmp/clio-lo-abcdefgh", uid=4294967295)
    long_dir = "/Users/someone/Library/Application Support/ai.iowarp.clio.desktop/lo"
    assert not office.pipe_dir_fits(long_dir, uid=501)
    with pytest.raises(ValueError, match="sun_path"):
        office.office_seatbelt_prefix(["/w"], "soffice", [], pipe_dir=long_dir)


def test_wrap_confined_owns_the_pipe_dir_on_darwin(roots, pipe_parent, monkeypatch) -> None:
    from clio_agent.runtime import sandbox_net

    monkeypatch.setattr(sys, "platform", "darwin")
    monkeypatch.setattr(sandbox_net, "open_child_egress", lambda state, roots: ("", None, {}))
    confined = sandbox.wrap_confined(
        "/opt/soffice",
        ["--headless", "--convert-to", "pdf", "a.docx"],
        write_roots=[str(roots["work"])],
        net_policy=sandbox.NET_DENY,
        profile=sandbox.PROFILE_SHELL,
        state=_CODEX,
        office=True,
    )
    (pipe_dir,) = confined.cleanup_dirs
    assert confined.env_overlay["OSL_SOCKET_PATH"] == pipe_dir
    assert f"-env:OSL_SOCKET_PATH={pipe_dir}" in confined.args
    assert Path(pipe_dir).is_dir()
    confined.release()
    assert not Path(pipe_dir).exists()
    confined.release()


@pytest.mark.parametrize(("platform", "office_spawn"), [("linux", True), ("darwin", False)])
def test_other_spawns_get_no_pipe_dir(roots, monkeypatch, platform, office_spawn) -> None:
    from clio_agent.runtime import sandbox_net

    monkeypatch.setattr(sys, "platform", platform)
    monkeypatch.setattr(sandbox_net, "open_child_egress", lambda state, roots: ("", None, {}))
    monkeypatch.setattr(
        office, "create_office_pipe_dir", lambda: pytest.fail("no pipe dir expected")
    )
    confined = sandbox.wrap_confined(
        "soffice",
        ["--headless"],
        write_roots=[str(roots["work"])],
        net_policy=sandbox.NET_DENY,
        profile=sandbox.PROFILE_SHELL,
        state=_CODEX,
        office=office_spawn,
    )
    assert confined.cleanup_dirs == ()
    assert "OSL_SOCKET_PATH" not in confined.env_overlay
    assert not any("OSL_SOCKET_PATH" in arg for arg in confined.args)


def test_office_convert_passes_the_pipe_dir_to_soffice(tmp_path, monkeypatch) -> None:
    from clio_agent.runtime.document_stack import process

    source = tmp_path / "a.docx"
    source.write_bytes(b"docx")
    seen: list[list[str]] = []

    def execute(command: list[str], cwd: Path) -> None:
        seen.append(command)
        (cwd / "a.pdf").write_bytes(b"%PDF")

    monkeypatch.setattr(process, "new_office_profile", lambda profile, template: None)
    monkeypatch.setenv("OSL_SOCKET_PATH", "/private/tmp/clio-lo-test")
    process.office_convert(source, tmp_path / "out", "pdf", execute=execute, executable_path="so")
    assert seen[0][:2] == ["so", "-env:OSL_SOCKET_PATH=/private/tmp/clio-lo-test"]
    monkeypatch.delenv("OSL_SOCKET_PATH")
    process.office_convert(source, tmp_path / "out2", "pdf", execute=execute, executable_path="so")
    assert not any("OSL_SOCKET_PATH" in arg for arg in seen[1])


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
