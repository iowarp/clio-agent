"""Runtime setup, verification, reuse and policy have deterministic failure behavior."""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from clio_agent.runtime import document_runtime as runtime
from clio_agent.tools.file_policy import FileAccessPolicy


def test_python_upgrade_and_platform_change_use_distinct_caches(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An upgrade cannot reuse a virtualenv built for another interpreter or host."""
    monkeypatch.setattr(runtime.sysconfig, "get_platform", lambda: "win-amd64")
    monkeypatch.setattr(
        runtime, "sys", SimpleNamespace(implementation=SimpleNamespace(cache_tag="cpython-312"))
    )
    old_cache = runtime._fingerprint()
    runtime.sys.implementation.cache_tag = "cpython-313"
    new_cache = runtime._fingerprint()
    assert new_cache != old_cache
    assert runtime._fingerprint() == new_cache
    monkeypatch.setattr(runtime.sysconfig, "get_platform", lambda: "linux-x86_64")
    assert runtime._fingerprint() != new_cache


def test_missing_uv_reports_execution_host_failure(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        FileAccessPolicy, "from_env", classmethod(lambda cls: FileAccessPolicy((tmp_path,)))
    )
    monkeypatch.setattr(runtime, "_document_uv", lambda: None)
    with pytest.raises(runtime.DocumentRuntimeError, match="uv is unavailable"):
        runtime.prepare_document_runtime(tmp_path, cache_root=tmp_path / "cache")


def test_import_preflight_checks_the_selected_interpreter_and_missing_dependencies(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Probe real import behavior, including a deterministic unavailable module."""
    import subprocess
    import sys

    seen: list[str] = []

    def run(command: list[str], **kwargs: Any) -> str:
        seen.extend(command)
        return subprocess.check_output(command, text=True, cwd=kwargs["cwd"])

    monkeypatch.setattr(runtime, "_run", run)
    checks = runtime.inspect_python_imports(
        Path(sys.executable), ["json", "_clio_test_module_not_installed", "json"]
    )
    assert seen[0] == sys.executable
    assert checks["json"] == {"status": "ready"}
    assert checks["_clio_test_module_not_installed"]["status"] == "missing"
    assert len(checks) == 2
    with pytest.raises(ValueError, match="module names"):
        runtime.inspect_python_imports(Path(sys.executable), ["json;print('unsafe')"])


def test_python_failure_never_advertises_ready_runtime(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        FileAccessPolicy, "from_env", classmethod(lambda cls: FileAccessPolicy((tmp_path,)))
    )
    monkeypatch.setattr(runtime, "_document_uv", lambda: "uv")

    def fail(cache: Path, uv: str) -> tuple[Path, dict[str, Any]]:
        raise runtime.DocumentRuntimeError("offline: dependency missing")

    monkeypatch.setattr(runtime, "_python_runtime", fail)
    with pytest.raises(runtime.DocumentRuntimeError, match="offline"):
        runtime.prepare_document_runtime(tmp_path, cache_root=tmp_path / "cache")


def test_javascript_failure_keeps_verified_python_and_reports_partial(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        FileAccessPolicy, "from_env", classmethod(lambda cls: FileAccessPolicy((tmp_path,)))
    )
    monkeypatch.setattr(runtime, "_document_uv", lambda: "uv")
    inventory = {
        "native_tools": {"soffice": {"status": "missing"}, "tesseract": {"status": "missing"}},
        "python": "prepared-python",
    }
    monkeypatch.setattr(runtime, "_python_runtime", lambda cache, uv: (cache / "python", inventory))

    def fail(*args: Any, **kwargs: Any) -> tuple[Path, Path]:
        raise runtime.DocumentRuntimeError("module resolution failed")

    monkeypatch.setattr(runtime, "_javascript_runtime", fail)
    result = runtime.prepare_document_runtime(tmp_path, cache_root=tmp_path / "cache")
    assert result["status"] == "partial"
    assert result["javascript"]["status"] == "failed"
    assert result["python_argv"][-1] == "python"
    assert result["capabilities"]["office_render_recalculate"] == "missing"
    assert Path(result["output_directory"]).is_relative_to(tmp_path)
    assert Path(result["output_directory"]).is_relative_to(tmp_path / ".tmp")


def test_python_cache_reuse_still_probes_imports(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import os

    python = tmp_path / "python" / ("Scripts/python.exe" if os.name == "nt" else "bin/python")
    python.parent.mkdir(parents=True)
    python.touch()
    monkeypatch.setattr(runtime, "_probe", lambda path: {"python": str(path), "verified": True})
    result, inventory = runtime._python_runtime(tmp_path, "missing-uv")
    assert result == python
    assert inventory["verified"]


def test_javascript_preserves_changed_task_manifest(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    cache = tmp_path / "cache"
    package = cache / "javascript-tools" / "node_modules" / "pnpm"
    package.mkdir(parents=True)
    (package / "package.json").write_text(
        json.dumps({"version": runtime.PNPM_VERSION, "bin": {"pnpm": "pnpm"}})
    )
    (package / "pnpm").touch()
    work = tmp_path / ".tmp" / "clio-documents" / "javascript"
    work.mkdir(parents=True)
    custom = '{"dependencies":{"custom":"1.0.0"}}'
    (work / "package.json").write_text(custom)
    monkeypatch.setattr(runtime, "_run", lambda *args, **kwargs: runtime.PNPM_VERSION)
    with pytest.raises(runtime.DocumentRuntimeError, match="manifest was changed"):
        runtime._javascript_runtime(cache, tmp_path, {"node": "node"})
    assert (work / "package.json").read_text() == custom


@pytest.mark.parametrize("damage", ["invalid-json", "wrong-version", "wrong-executable"])
def test_damaged_pnpm_gets_one_verified_pinned_repair(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, damage: str
) -> None:
    package = tmp_path / "cache/javascript-tools/node_modules/pnpm"
    package.mkdir(parents=True)
    metadata = package / "package.json"
    metadata.write_text(
        "invalid"
        if damage == "invalid-json"
        else json.dumps(
            {
                "version": "old" if damage == "wrong-version" else runtime.PNPM_VERSION,
                "bin": {"pnpm": "pnpm"},
            }
        )
    )
    (package / "pnpm").touch()
    node = tmp_path / "managed node" / "node.exe"
    npm = node.parent / "lib/node_modules/npm/bin/npm-cli.js"
    npm.parent.mkdir(parents=True)
    npm.touch()
    work = tmp_path / ".tmp/clio-documents/javascript"
    (work / "node_modules").mkdir(parents=True)
    installs: list[list[str]] = []

    def run(command: list[str], **kwargs: Any) -> str:
        if "--prefix" in command:
            installs.append(command)
            metadata.write_text(
                json.dumps({"version": runtime.PNPM_VERSION, "bin": {"pnpm": "pnpm"}})
            )
        if "--version" in command:
            return "old" if damage == "wrong-executable" and not installs else runtime.PNPM_VERSION
        return ""

    monkeypatch.setattr(runtime, "_run", run)
    prepared, pnpm = runtime._javascript_runtime(tmp_path / "cache", tmp_path, {"node": str(node)})
    assert prepared == work
    assert pnpm == package / "pnpm"
    assert len(installs) == 1
    assert f"pnpm@{runtime.PNPM_VERSION}" in installs[0]
    assert installs[0][:2] == [str(node), str(npm)]


def test_failed_helper_exit_retains_manifest_result(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from types import SimpleNamespace

    from clio_agent.runtime import sandbox

    seen: dict[str, Any] = {}

    def run(command: list[str], **kwargs: Any) -> str:
        seen.update(kwargs)
        assert command == ["confined-python", "helper.py", "render", "input.docx"]
        return json.dumps({"status": "failed", "manifest": "output/manifest.json"})

    monkeypatch.setattr(runtime, "_run", run)
    monkeypatch.setattr(
        sandbox,
        "wrap_confined",
        lambda command, args, **kwargs: SimpleNamespace(
            command="confined-python", args=args, env_overlay={"TEMP": "backend-cache"}
        ),
    )
    result = runtime.run_document_helper(
        {
            "helper_argv": ["python", "helper.py"],
            "shell_environment": {"TEMP": str(tmp_path / ".tmp")},
        },
        ["render", "input.docx"],
        cwd=tmp_path,
    )
    assert result["status"] == "failed"
    assert seen["accepted_exit_codes"] == (0, 1)
    assert seen["env"]["TEMP"] == str(tmp_path / ".tmp")
