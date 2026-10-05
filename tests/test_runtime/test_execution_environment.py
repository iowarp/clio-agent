"""Prepared tools are scoped to the workspace and preserve project environments."""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from clio_agent.runtime import execution_environment as execution


def test_prepared_shell_uses_uv_python_and_pnpm_without_host_tools(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    cache = tmp_path / "cache"
    workspace.mkdir()
    cache.mkdir()
    runtime = {
        "uv": str(tmp_path / "bundled" / "uv"),
        "python": str(cache / "python"),
        "node": str(cache / "node"),
        "office_root": str(cache / "office"),
        "python_argv": [
            "bundled uv",
            "run",
            "--no-project",
            "--python",
            "prepared python",
            "python",
        ],
        "javascript": {"status": "ready", "pnpm_argv": ["prepared node", "pinned pnpm"]},
    }
    execution.publish_environment(workspace, runtime, cache)
    environment = execution.shell_environment(workspace)
    assert environment["CLIO_EXECUTION_PYTHON"] == str(cache / "python")
    assert "UV_PYTHON" not in environment
    assert environment["PATH"].split(os.pathsep)[0] == str(cache / "shell-bin")
    assert execution.shell_environment(tmp_path / "different") == {}
    scratch = workspace / ".tmp" / "clio-documents"
    assert Path(runtime["scratch_directory"]) == scratch
    assert scratch.is_dir()
    for key in ("TEMP", "TMP", "TMPDIR", "CLIO_DOCUMENT_SCRATCH"):
        assert Path(environment[key]) == scratch
    suffix = ".cmd" if os.name == "nt" else ""
    assert "--no-project" in (cache / "shell-bin" / f"python{suffix}").read_text()
    assert "pinned pnpm" in (cache / "shell-bin" / f"pnpm{suffix}").read_text()
    environment["PATH"] = "untrusted"
    assert execution.shell_environment(workspace)["PATH"] != "untrusted"


def test_bundle_discovery_survives_relocation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    (tmp_path / "runtime.json").write_text("{}")
    monkeypatch.delenv("GACT_BUNDLED_RUNTIME_DIR", raising=False)
    monkeypatch.setattr(
        execution.sys, "executable", str(tmp_path / "python" / "bin" / "python3.12")
    )
    assert execution.bundled_root() == tmp_path


def test_login_shell_restores_managed_tools_and_keeps_host_profile_path() -> None:
    command = execution.posix_command(
        "python script.py", {"CLIO_EXECUTION_PATH": "/managed tools/bin:/node/bin"}
    )
    assert command == "export PATH='/managed tools/bin:/node/bin':\"$PATH\"\npython script.py"
    assert execution.posix_command("python script.py", {}) == "python script.py"


def test_workspace_scratch_refuses_a_redirect_outside_the_workspace(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from clio_agent.runtime.document_stack.process import DocumentError, scratch_root

    workspace = tmp_path / "workspace"
    workspace.mkdir()
    outside = tmp_path / "outside"
    original = Path.resolve

    def resolve(path: Path, strict: bool = False) -> Path:
        if path == workspace / ".tmp" / "clio-documents":
            return outside
        return original(path, strict=strict)

    monkeypatch.setattr(Path, "resolve", resolve)
    with pytest.raises(DocumentError, match="escapes the active workspace"):
        scratch_root(workspace)
    assert not outside.exists()


def test_scratch_path_can_be_checked_before_creating_it(tmp_path: Path) -> None:
    from clio_agent.runtime.document_stack.process import scratch_root

    root = scratch_root(tmp_path, create=False)
    assert root == tmp_path / ".tmp" / "clio-documents"
    assert not root.exists()
