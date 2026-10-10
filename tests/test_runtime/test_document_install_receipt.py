"""Startup reuses real package-file identities, with invalidation and policy checks."""

from __future__ import annotations

import copy
import json
import os
import sys
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from clio_agent.platform_paths import win_extended_path
from clio_agent.runtime import document_install as installer
from clio_agent.runtime import document_install_receipt as receipts
from clio_agent.runtime.document_stack.process import scratch_root
from clio_agent.runtime.execution_environment import shell_environment


@pytest.fixture
def installed(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> tuple[Path, Path, dict[str, Any]]:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    cache = tmp_path / "cache"
    runtime = cache / "locked"
    python = runtime / "python/Scripts/python.exe"
    javascript = scratch_root(workspace) / "javascript"
    pnpm = runtime / "tools/node_modules/pnpm/bin/pnpm.cjs"
    office = tmp_path / "office/program/soffice"
    uv = tmp_path / "uv"
    gh = tmp_path / "gh"
    interpreter = tmp_path / "interpreter"
    stack = tmp_path / "stack"
    for path in (
        python,
        python.parent.parent / "Lib/site-packages/example/__init__.py",
        javascript / "node_modules/example/index.js",
        javascript / "package.json",
        javascript / "pnpm-lock.yaml",
        pnpm,
        office,
        uv,
        gh,
        interpreter,
        stack / "inventory.py",
    ):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("fixture", encoding="utf-8")
    output = scratch_root(workspace) / "output"
    output.mkdir()
    monkeypatch.setattr(receipts, "_fingerprint", lambda: "locked")
    monkeypatch.setattr(receipts, "STACK_ROOT", stack)
    monkeypatch.setattr(receipts, "office_runtime_root", lambda: office.parent.parent)
    monkeypatch.setattr(
        receipts, "sys", SimpleNamespace(executable=str(interpreter), version="test")
    )
    monkeypatch.setenv("CLIO_ALLOWED_ROOTS", str(workspace))
    result: dict[str, Any] = {
        "status": "ready",
        "runtime_id": "locked",
        "python": str(python),
        "python_argv": [str(python)],
        "uv": str(uv),
        "node": str(python),
        "office_root": str(office.parent.parent),
        "output_directory": str(output),
        "javascript": {
            "status": "ready",
            "workspace": str(javascript),
            "pnpm_argv": [str(python), str(pnpm)],
        },
        "native_tools": {
            "soffice": {"status": "available", "path": str(office)},
            "gh": {"status": "available", "path": str(gh)},
        },
        "capabilities": {},
    }
    return workspace, cache, result


def test_unchanged_installation_reuses_inventory_and_ignores_generated_bytecode(
    installed: tuple[Path, Path, dict[str, Any]],
) -> None:
    workspace, cache, result = installed
    receipts.save_install_receipt(workspace, result, cache_root=cache)
    bytecode = Path(result["python"]).parent.parent / "Lib/site-packages/example/__pycache__/a.pyc"
    bytecode.parent.mkdir()
    bytecode.write_bytes(b"generated")
    assert receipts.reuse_install_receipt(workspace, cache_root=cache) == result


def test_install_and_startup_accept_equivalent_runtime_and_workspace_paths(
    installed: tuple[Path, Path, dict[str, Any]], monkeypatch: pytest.MonkeyPatch
) -> None:
    workspace, cache, result = installed
    runtime = Path(result["python"]).parent.parent
    interpreter = receipts.sys.executable
    monkeypatch.setenv("GACT_BUNDLED_RUNTIME_DIR", str(runtime))
    receipts.save_install_receipt(workspace, result, cache_root=cache)
    aliases = [workspace / ".." / workspace.name]
    if os.name == "nt":
        aliases.append(Path(win_extended_path(workspace)))
    for alias in aliases:
        monkeypatch.setenv(
            "GACT_BUNDLED_RUNTIME_DIR", win_extended_path(runtime / ".." / runtime.name)
        )
        monkeypatch.setattr(receipts.sys, "executable", win_extended_path(interpreter))
        assert receipts.reuse_install_receipt(alias, cache_root=cache) == result


@pytest.mark.parametrize("change", ["edit", "remove", "add", "office", "manifest", "helper"])
def test_changed_required_files_invalidate_preparation(
    installed: tuple[Path, Path, dict[str, Any]], change: str
) -> None:
    workspace, cache, result = installed
    receipts.save_install_receipt(workspace, result, cache_root=cache)
    package = Path(result["python"]).parent.parent / "Lib/site-packages/example/__init__.py"
    if change == "remove":
        package.unlink()
    elif change == "add":
        package.with_name("new.py").write_text("added")
    else:
        target = {
            "edit": package,
            "office": Path(result["native_tools"]["soffice"]["path"]),
            "manifest": Path(result["javascript"]["workspace"]) / "package.json",
            "helper": receipts.STACK_ROOT / "inventory.py",
        }[change]
        target.write_text("changed package bytes")
    assert receipts.reuse_install_receipt(workspace, cache_root=cache) is None


@pytest.mark.parametrize("change", ["workspace", "locks", "interpreter", "environment"])
def test_different_installation_context_needs_preparation(
    installed: tuple[Path, Path, dict[str, Any]], monkeypatch: pytest.MonkeyPatch, change: str
) -> None:
    workspace, cache, result = installed
    receipts.save_install_receipt(workspace, result, cache_root=cache)
    if change == "workspace":
        workspace = workspace.parent / "other"
        workspace.mkdir()
    elif change == "locks":
        monkeypatch.setattr(receipts, "_fingerprint", lambda: "new-locks")
    elif change == "interpreter":
        monkeypatch.setattr(receipts.sys, "version", "updated")
    else:
        monkeypatch.setenv("CLIO_DOCUMENT_SOFFICE", "different renderer")
    assert receipts.reuse_install_receipt(workspace, cache_root=cache) is None


@pytest.mark.parametrize("payload", ["[]", "null", "{", "{}", '{"installation_check":null}'])
def test_old_or_malformed_receipt_never_shortcuts_setup(
    installed: tuple[Path, Path, dict[str, Any]], payload: str
) -> None:
    workspace, cache, _ = installed
    (cache / "locked/installed.json").write_text(payload)
    assert receipts.reuse_install_receipt(workspace, cache_root=cache) is None


def test_receipt_metadata_tampering_invalidates_inventory(
    installed: tuple[Path, Path, dict[str, Any]],
) -> None:
    workspace, cache, result = installed
    receipts.save_install_receipt(workspace, result, cache_root=cache)
    path = cache / "locked/installed.json"
    payload = json.loads(path.read_text())
    payload["status"] = "partial"
    path.write_text(json.dumps(payload))
    assert receipts.reuse_install_receipt(workspace, cache_root=cache) is None


@pytest.mark.parametrize("explicit_setup", [False, True])
def test_reuse_preserves_fresh_sandbox_check_and_workspace_bindings(
    installed: tuple[Path, Path, dict[str, Any]],
    monkeypatch: pytest.MonkeyPatch,
    explicit_setup: bool,
) -> None:
    workspace, cache, result = installed
    receipts.save_install_receipt(workspace, result, cache_root=cache)
    preparations: list[str] = []

    def packages(*args: Any, **kwargs: Any) -> dict[str, Any]:
        preparations.append("packages")
        return copy.deepcopy(result)

    def fence(**kwargs: Any) -> dict[str, str]:
        assert kwargs["allow_elevation"] is explicit_setup
        return {"status": "available", "implementation": "mxc", "check": "fresh"}

    monkeypatch.setattr(installer, "prepare_document_runtime", packages)
    monkeypatch.setattr(
        installer, "prepare_office_runtime", lambda: result["native_tools"]["soffice"]["path"]
    )
    monkeypatch.setattr(
        installer, "ensure_github_cli", lambda: Path(result["native_tools"]["gh"]["path"])
    )
    monkeypatch.setattr(installer, "prepare_existing_windows_fence", fence)
    monkeypatch.setattr(
        installer,
        "ensure_bundled_codex_windows_helpers",
        lambda: pytest.fail("MXC needs no legacy helpers"),
    )
    actual = installer.install_document_runtime(
        workspace, cache_root=cache, reuse_installed=True, setup_protected_execution=explicit_setup
    )
    assert preparations == (["packages"] if explicit_setup else [])
    assert actual["native_tools"]["protected_execution"]["check"] == "fresh"
    if not explicit_setup:
        assert shell_environment(workspace)["CLIO_EXECUTION_PYTHON"] == result["python"]


def test_missing_cache_repairs_with_full_preparation(
    installed: tuple[Path, Path, dict[str, Any]], monkeypatch: pytest.MonkeyPatch
) -> None:
    workspace, cache, result = installed
    calls: list[str] = []

    def packages(*args: Any, **kwargs: Any) -> dict[str, Any]:
        calls.append("prepare")
        return result

    monkeypatch.setattr(installer, "prepare_document_runtime", packages)
    monkeypatch.setattr(
        installer, "prepare_office_runtime", lambda: result["native_tools"]["soffice"]["path"]
    )
    monkeypatch.setattr(
        installer, "ensure_github_cli", lambda: Path(result["native_tools"]["gh"]["path"])
    )
    monkeypatch.setattr(
        installer,
        "prepare_existing_windows_fence",
        lambda **kw: {"status": "available", "implementation": "mxc"},
    )
    installer.install_document_runtime(workspace, cache_root=cache, reuse_installed=True)
    assert calls == ["prepare"]
    assert receipts.reuse_install_receipt(workspace, cache_root=cache) == result


@pytest.mark.parametrize("reuse", [False, True])
def test_startup_cli_forwards_reuse_request(
    installed: tuple[Path, Path, dict[str, Any]], monkeypatch: pytest.MonkeyPatch, reuse: bool
) -> None:
    workspace, _, result = installed
    arguments = ["document_install", "--workspace", str(workspace)]
    if reuse:
        arguments.append("--reuse-installed")
    monkeypatch.setattr(sys, "argv", arguments)

    def install(*args: Any, **kwargs: Any) -> dict[str, Any]:
        assert kwargs["reuse_installed"] is reuse
        assert kwargs["setup_protected_execution"] is False
        return result

    monkeypatch.setattr(installer, "install_document_runtime", install)
    installer.main()
