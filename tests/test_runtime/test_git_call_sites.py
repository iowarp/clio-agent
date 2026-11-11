"""Every git CLIO runs goes through the one resolver (``runtime.git_executable``)."""

from __future__ import annotations

import builtins
import json
import os
import subprocess
from pathlib import Path
from typing import Any

import pytest

from clio_agent.gact.infrastructure import (
    controller_source,
    native_monitoring_hook,
    node_service_stack,
)
from clio_agent.plugins import sources
from clio_agent.runtime import execution_environment as execution
from clio_agent.runtime import git_executable as gx
from clio_agent.tools.mcp_environment import stdio_environment

posix_only = pytest.mark.skipif(os.name == "nt", reason="shell-script stand-in for git")


def _runtime(tmp_path: Path) -> Path:
    runtime = tmp_path / "gact-runtime"
    git = runtime / "git" / gx.BUNDLED_GIT_RELATIVE
    git.parent.mkdir(parents=True)
    git.write_text('#!/bin/sh\necho "$@" >> "$CLIO_TEST_GIT_LOG"\n', encoding="utf-8")
    git.chmod(0o755)
    (runtime / "runtime.json").write_text(json.dumps({"schema": 1}))
    return runtime


@pytest.fixture
def bundled(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> tuple[Path, Path]:
    """A bundled git that logs its argv, and a PATH with no git at all."""
    runtime = _runtime(tmp_path)
    empty = tmp_path / "empty-path"
    empty.mkdir()
    log = tmp_path / "git.log"
    monkeypatch.delenv(gx.GIT_OVERRIDE_ENV, raising=False)
    monkeypatch.setenv("PATH", str(empty))
    monkeypatch.setenv("CLIO_TEST_GIT_LOG", str(log))
    monkeypatch.setenv("GIT_EXEC_PATH", "/another/git-core")
    monkeypatch.setattr(execution, "bundled_root", lambda: runtime)
    return runtime / "git" / gx.BUNDLED_GIT_RELATIVE, log


@pytest.fixture
def no_git(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.delenv(gx.GIT_OVERRIDE_ENV, raising=False)
    monkeypatch.setenv("PATH", str(tmp_path))
    monkeypatch.setattr(execution, "bundled_root", lambda: None)


@posix_only
def test_marketplace_sources_run_the_bundled_git(
    bundled: tuple[Path, Path], tmp_path: Path
) -> None:
    _, log = bundled
    sources._git(["init", "-q", str(tmp_path / "repo")])
    assert log.read_text().split() == ["init", "-q", str(tmp_path / "repo")]


@posix_only
def test_marketplace_sources_prefer_the_persons_git(
    bundled: tuple[Path, Path], tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _, log = bundled
    theirs = tmp_path / "their-bin"
    theirs.mkdir()
    own = theirs / "git"
    own.write_text('#!/bin/sh\necho "theirs $GIT_EXEC_PATH $*" >> "$CLIO_TEST_GIT_LOG"\n')
    own.chmod(0o755)
    monkeypatch.setenv("PATH", str(theirs))
    sources._git(["fetch", "origin"])
    assert log.read_text().split() == ["theirs", "/another/git-core", "fetch", "origin"]


def test_marketplace_sources_report_a_missing_git_as_a_source_error(no_git: None) -> None:
    with pytest.raises(sources.SourceError, match="git is not installed"):
        sources._git(["fetch", "origin"])


@posix_only
def test_controller_source_reads_its_commit_through_the_resolver(
    bundled: tuple[Path, Path], tmp_path: Path
) -> None:
    _, log = bundled
    controller_source._git(tmp_path, "rev-parse", "HEAD")
    assert log.read_text().split() == ["-C", str(tmp_path), "rev-parse", "HEAD"]


def test_controller_source_without_git_has_no_commit(no_git: None, tmp_path: Path) -> None:
    assert controller_source._git(tmp_path, "rev-parse", "HEAD") == ""


@pytest.mark.parametrize("module", [node_service_stack, native_monitoring_hook])
def test_target_helpers_use_the_resolver_inside_clio(
    module: Any, bundled: tuple[Path, Path]
) -> None:
    git, _ = bundled
    argv, env = module._git_command("clone", "u", "d")
    assert argv == [str(git), "clone", "u", "d"]
    assert env is not None and "GIT_EXEC_PATH" not in env


@pytest.mark.parametrize("module", [node_service_stack, native_monitoring_hook])
def test_target_helpers_use_the_host_git_without_clio(
    module: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    real_import = builtins.__import__

    def without_clio(name: str, *args: Any, **kwargs: Any) -> Any:
        if name.startswith("clio_agent"):
            raise ImportError(name)
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", without_clio)
    assert module._git_command("checkout", "abc") == (["git", "checkout", "abc"], None)


@posix_only
def test_node_service_checkout_runs_the_bundled_git(
    bundled: tuple[Path, Path], tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    git, _ = bundled
    calls: list[tuple[list[str], dict[str, str] | None]] = []

    def record(argv: list[str], **kwargs: Any) -> subprocess.CompletedProcess[bytes]:
        calls.append((argv, kwargs.get("env")))
        return subprocess.CompletedProcess(argv, 0)

    monkeypatch.setattr(node_service_stack.subprocess, "run", record)
    root = tmp_path / "service"
    root.mkdir()
    node_service_stack.checkout_source(root, {"repository": "https://x/r.git", "revision": "a"})
    assert [argv[0] for argv, _ in calls] == [str(git), str(git)]
    assert [argv[1] for argv, _ in calls] == ["clone", "-C"]
    assert all(env is not None and "GIT_EXEC_PATH" not in env for _, env in calls)


@posix_only
def test_mcp_servers_get_the_bundled_git_on_path(bundled: tuple[Path, Path]) -> None:
    git, _ = bundled
    env = stdio_environment({}, command="uvx", args=["git+https://example.invalid/x"])
    assert env["PATH"].split(os.pathsep)[0] == str(git.parent)
    # Blanked, not absent: the process's own GIT_EXEC_PATH must not survive a merge.
    assert env["GIT_EXEC_PATH"] == ""


@posix_only
def test_workspace_shells_get_the_bundled_git_on_path(
    bundled: tuple[Path, Path], tmp_path: Path
) -> None:
    git, _ = bundled
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    environment = execution.shell_environment(workspace)
    assert environment["PATH"].split(os.pathsep)[0] == str(git.parent)
    assert environment["CLIO_EXECUTION_PATH"].split(os.pathsep)[0] == str(git.parent)
