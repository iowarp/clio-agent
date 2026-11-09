"""Local native services build on CLIO's own Python; no second Python, no CLT stub.

Stdlib-only: the lifecycle CI job runs this without the application dependencies.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

from clio_agent.gact.infrastructure import native_service_platform as platform


def test_uv_builds_on_the_controller_interpreter_without_downloads() -> None:
    assert platform.uv_python_arguments({"interpreter": "clio", "python_version": "3.12"}) == [
        "--python",
        sys.executable,
        "--no-python-downloads",
    ]


@pytest.mark.parametrize(
    ("system", "nt", "managed"),
    [("darwin", False, True), ("linux", False, False), ("win32", True, True)],
)
def test_otherwise_uv_takes_the_manifest_python(
    monkeypatch: pytest.MonkeyPatch, system: str, nt: bool, managed: bool
) -> None:
    monkeypatch.setattr(platform.sys, "platform", system)
    monkeypatch.setattr(platform.os, "name", "nt" if nt else "posix")
    arguments = platform.uv_python_arguments({"python_version": "3.10"})
    assert arguments == ["--python", "3.10", *(["--managed-python"] if managed else [])]


def test_a_vanishing_interpreter_falls_back_to_the_pinned_python(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(platform, "durable_interpreter", lambda executable=None: False)
    monkeypatch.setattr(platform.sys, "platform", "darwin")
    assert platform.uv_python_arguments({"interpreter": "clio"}) == [
        "--python",
        "3.12",
        "--managed-python",
    ]


@pytest.mark.parametrize(
    ("executable", "durable"),
    [
        ("/Users/me/Applications/CLIO Desktop.app/Contents/Resources/python/bin/python3", True),
        ("/private/var/folders/x/AppTranslocation/ABC/d/CLIO.app/python/bin/python3", False),
        (r"C:\Users\me\AppData\Local\Microsoft\WindowsApps\python.exe", False),
        ("/Volumes/CLIO Desktop/CLIO Desktop.app/Contents/Resources/python/bin/python3", False),
        ("", False),
    ],
)
def test_only_an_interpreter_that_stays_put_is_durable(executable: str, durable: bool) -> None:
    assert platform.durable_interpreter(executable) is durable


def test_without_the_command_line_tools_install_name_tool_is_shadowed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(platform.sys, "platform", "darwin")
    monkeypatch.setattr(platform, "_MACOS_TOOLCHAINS", (str(tmp_path / "missing"),))
    tools = platform.clt_free_tools(tmp_path)
    assert tools == str(tmp_path / "cache/clt-free-bin")
    result = subprocess.run(
        [str(Path(tools) / "install_name_tool"), "-id", "x", "y"],
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 1 and "Command Line Tools" in result.stderr
    env = platform.worker_environment(tmp_path, {}, {"PATH": "/usr/bin:/bin"})
    assert env["PATH"].split(platform.os.pathsep)[1:] == [tools, "/usr/bin", "/bin"]


def test_a_real_toolchain_or_another_os_is_left_alone(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    toolchain = tmp_path / "usr/bin"
    toolchain.mkdir(parents=True)
    (toolchain / "install_name_tool").write_text("")
    monkeypatch.setattr(platform, "_MACOS_TOOLCHAINS", (str(toolchain),))
    monkeypatch.setattr(platform.sys, "platform", "darwin")
    assert platform.clt_free_tools(tmp_path) == ""
    monkeypatch.setattr(platform.sys, "platform", "linux")
    monkeypatch.setattr(platform, "_MACOS_TOOLCHAINS", ())
    assert platform.clt_free_tools(tmp_path) == ""
    assert not (tmp_path / "cache/clt-free-bin").exists()
