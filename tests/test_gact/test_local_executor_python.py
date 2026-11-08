"""CLIO's local ``python3`` node scripts run on CLIO's own interpreter, not the system one."""

from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

from clio_agent.gact.infrastructure.local_executor import local_environment, run_local
from clio_agent.gact.infrastructure.models import CommandSpec


def test_local_environment_leads_path_with_clio_interpreter() -> None:
    env = local_environment({"PATH": "/usr/bin:/bin"})

    assert env["PATH"].split(os.pathsep)[0] == os.path.dirname(sys.executable)
    assert env["PATH"].endswith("/usr/bin:/bin")


@pytest.mark.skipif(os.name == "nt", reason="POSIX shim directory")
async def test_python3_resolves_to_clio_interpreter_over_an_older_system_python(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # An HPC node whose system python3 is too old for CLIO's node scripts.
    shim = tmp_path / "python3"
    shim.write_text("#!/bin/sh\necho system-python-3.6\n", encoding="utf-8")
    shim.chmod(0o755)
    monkeypatch.setenv("PATH", f"{tmp_path}{os.pathsep}{os.environ.get('PATH', '')}")
    if not (Path(sys.executable).parent / "python3").exists():
        pytest.skip("CLIO's interpreter directory has no python3 entry point")

    result = await run_local(
        CommandSpec(
            program="python3",
            args=["-c", "import sys; print(sys.version_info[:2])"],
            timeout_seconds=30,
        )
    )

    assert result.exit_code == 0
    assert result.stdout.strip() == str(tuple(sys.version_info[:2]))


@pytest.mark.skipif(os.name == "nt", reason="POSIX shim directory")
async def test_python3_is_clio_interpreter_even_when_its_directory_has_no_python3(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # A bundled runtime whose bin/ holds only python3.X; /usr/bin/python3 on a Mac
    # without the Command Line Tools is a stub that opens an install dialog.
    runtime_bin = tmp_path / "runtime" / "bin"
    runtime_bin.mkdir(parents=True)
    interpreter = runtime_bin / f"python{sys.version_info[0]}.{sys.version_info[1]}"
    interpreter.symlink_to(os.path.realpath(sys.executable))
    stub_dir = tmp_path / "usr-bin"
    stub_dir.mkdir()
    stub = stub_dir / "python3"
    stub.write_text("#!/bin/sh\necho clt-stub-dialog\nexit 1\n", encoding="utf-8")
    stub.chmod(0o755)
    monkeypatch.setattr(sys, "executable", str(interpreter))
    monkeypatch.setenv("PATH", f"{stub_dir}{os.pathsep}/bin")

    result = await run_local(
        CommandSpec(program="python3", args=["-c", "print('clio')"], timeout_seconds=30)
    )

    assert result.exit_code == 0, result.stderr
    assert result.stdout.strip() == "clio"
