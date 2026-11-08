"""CLIO resolves one git: override, then its bundled git, then a real PATH git.

A fresh Mac's ``/usr/bin/git`` is the developer-tools placeholder; running it opens an
installer dialog, so the resolver must recognise it without executing it.
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

import pytest

from clio_agent.runtime import execution_environment as execution
from clio_agent.runtime import git_executable as gx

posix_only = pytest.mark.skipif(os.name == "nt", reason="shell-script stand-in for git")


def _script(path: Path, body: str) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(f"#!/bin/sh\n{body}\n", encoding="utf-8")
    path.chmod(0o755)
    return path


def _runtime(tmp_path: Path, *, with_git: bool = True) -> Path:
    """A desktop runtime folder (``runtime.json``) optionally holding the bundled git."""
    runtime = tmp_path / "gact-runtime"
    runtime.mkdir()
    (runtime / "runtime.json").write_text(json.dumps({"schema": 1, "exec": []}))
    if with_git:
        _script(runtime / "git" / gx.BUNDLED_GIT_RELATIVE, 'echo bundled "$@"')
    return runtime


@pytest.fixture
def clean_env(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Path:
    """No override, no bundled runtime, and an empty PATH directory."""
    empty = tmp_path / "empty-path"
    empty.mkdir()
    monkeypatch.delenv(gx.GIT_OVERRIDE_ENV, raising=False)
    monkeypatch.delenv("GIT_EXEC_PATH", raising=False)
    monkeypatch.setenv("PATH", str(empty))
    monkeypatch.setattr(execution, "bundled_root", lambda: None)
    return empty


def _use_runtime(monkeypatch: pytest.MonkeyPatch, runtime: Path) -> None:
    monkeypatch.setattr(execution, "bundled_root", lambda: runtime)


def test_bundled_git_is_found_in_the_runtime_folder(tmp_path: Path) -> None:
    runtime = _runtime(tmp_path)
    assert gx.bundled_git_root(runtime) == runtime / "git"
    assert gx.bundled_git(runtime) == runtime / "git" / gx.BUNDLED_GIT_RELATIVE


def test_a_runtime_without_git_has_no_bundled_git(tmp_path: Path) -> None:
    assert gx.bundled_git(_runtime(tmp_path, with_git=False)) is None


def test_bundled_root_follows_the_desktop_runtime_variable(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    runtime = _runtime(tmp_path)
    monkeypatch.setenv("GACT_BUNDLED_RUNTIME_DIR", str(runtime))
    assert gx.bundled_git() == runtime / "git" / gx.BUNDLED_GIT_RELATIVE


@posix_only
def test_override_wins_over_bundled_and_path(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, clean_env: Path
) -> None:
    chosen = _script(tmp_path / "chosen" / "git", "echo chosen")
    _script(clean_env / "git", "echo path")
    _use_runtime(monkeypatch, _runtime(tmp_path))
    monkeypatch.setenv(gx.GIT_OVERRIDE_ENV, str(chosen))
    git = gx.resolve_git()
    assert (git.source, git.path) == ("override", chosen)
    assert git.env() is None


def test_an_override_that_is_not_executable_is_refused(
    monkeypatch: pytest.MonkeyPatch, clean_env: Path
) -> None:
    monkeypatch.setenv(gx.GIT_OVERRIDE_ENV, str(clean_env / "missing-git"))
    with pytest.raises(gx.GitUnavailableError, match=gx.GIT_OVERRIDE_ENV):
        gx.resolve_git()


@posix_only
def test_bundled_git_wins_over_path_git_and_drops_a_foreign_exec_path(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, clean_env: Path
) -> None:
    _script(clean_env / "git", "echo path")
    runtime = _runtime(tmp_path)
    _use_runtime(monkeypatch, runtime)
    monkeypatch.setenv("GIT_EXEC_PATH", "/another/git/libexec/git-core")
    git = gx.resolve_git()
    assert (git.source, git.path) == ("bundled", runtime / "git" / gx.BUNDLED_GIT_RELATIVE)
    env = git.env()
    assert env is not None and "GIT_EXEC_PATH" not in env
    assert env["PATH"] == str(clean_env)


@posix_only
def test_a_real_path_git_is_used_without_a_bundled_one(clean_env: Path) -> None:
    on_path = _script(clean_env / "git", "echo path")
    git = gx.resolve_git()
    assert (git.source, git.path) == ("path", on_path)


def test_no_git_anywhere_says_what_to_install(clean_env: Path) -> None:
    with pytest.raises(gx.GitUnavailableError, match="git is not installed"):
        gx.resolve_git()


@pytest.fixture
def macos_placeholder(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> tuple[Path, Path]:
    """A stand-in ``/usr/bin/git`` placeholder that records any attempt to run it."""
    marker = tmp_path / "placeholder-ran"
    usr_bin = tmp_path / "usr" / "bin"
    placeholder = _script(usr_bin / "git", f'touch "{marker}"')
    developer = tmp_path / "Developer"
    monkeypatch.setattr(sys, "platform", "darwin")
    monkeypatch.setattr(gx, "_MACOS_PLACEHOLDER", placeholder.resolve())
    monkeypatch.setattr(gx, "_MACOS_DEVELOPER_DIRS", (developer,))
    monkeypatch.setattr(gx, "_XCODE_SELECT_LINK", tmp_path / "no-xcode-select-link")
    monkeypatch.delenv("DEVELOPER_DIR", raising=False)
    return placeholder, marker


@posix_only
def test_the_macos_placeholder_is_never_chosen_or_run(
    monkeypatch: pytest.MonkeyPatch, clean_env: Path, macos_placeholder: tuple[Path, Path]
) -> None:
    placeholder, marker = macos_placeholder
    monkeypatch.setenv("PATH", str(placeholder.parent))
    assert gx.is_macos_placeholder(placeholder)
    assert gx.real_git_on_path() is None
    with pytest.raises(gx.GitUnavailableError, match="placeholder"):
        gx.resolve_git()
    assert not marker.exists()


@posix_only
def test_the_macos_placeholder_is_real_once_developer_tools_exist(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, macos_placeholder: tuple[Path, Path]
) -> None:
    placeholder, marker = macos_placeholder
    _script(tmp_path / "Developer" / "usr" / "bin" / "git", "echo real")
    assert not gx.is_macos_placeholder(placeholder)
    selected = tmp_path / "Selected"
    _script(selected / "usr" / "bin" / "git", "echo real")
    monkeypatch.setattr(gx, "_MACOS_DEVELOPER_DIRS", ())
    assert gx.is_macos_placeholder(placeholder)
    monkeypatch.setenv("DEVELOPER_DIR", str(selected))
    assert not gx.is_macos_placeholder(placeholder)
    assert not marker.exists()


def test_other_platforms_have_no_placeholder(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(sys, "platform", "linux")
    assert not gx.is_macos_placeholder(gx._MACOS_PLACEHOLDER)


def test_linux_bundled_git_is_given_a_ca_bundle(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    bundle = tmp_path / "ca.pem"
    bundle.write_text("certs")
    monkeypatch.setattr(sys, "platform", "linux")
    monkeypatch.setattr(gx, "_LINUX_CA_BUNDLES", (str(tmp_path / "absent.pem"), str(bundle)))
    assert gx.bundled_git_env({}) == {"GIT_SSL_CAINFO": str(bundle)}
    assert gx.bundled_git_env({"GIT_SSL_CAINFO": "/mine.pem"}) == {}


def test_linux_falls_back_to_certifi(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    certifi = pytest.importorskip("certifi")
    monkeypatch.setattr(sys, "platform", "linux")
    monkeypatch.setattr(gx, "_LINUX_CA_BUNDLES", (str(tmp_path / "absent.pem"),))
    assert gx.bundled_git_env({}) == {"GIT_SSL_CAINFO": certifi.where()}


@pytest.mark.parametrize("platform", ["darwin", "win32"])
def test_macos_and_windows_need_no_ca_variable(
    monkeypatch: pytest.MonkeyPatch, platform: str
) -> None:
    monkeypatch.setattr(sys, "platform", platform)
    assert gx.bundled_git_env({}) == {}


def test_git_env_keeps_the_callers_values_and_fills_gaps() -> None:
    git = gx.GitExecutable(
        Path("/g"), "bundled", {"GIT_SSL_CAINFO": "/ca", "A": "x"}, ("GIT_EXEC_PATH",)
    )
    env = git.env({"A": "mine", "GIT_EXEC_PATH": "/other", "PATH": "/bin"})
    assert env == {"A": "mine", "GIT_SSL_CAINFO": "/ca", "PATH": "/bin"}
    assert git.command("clone", "u") == ["/g", "clone", "u"]


@posix_only
def test_run_git_runs_the_resolved_git(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, clean_env: Path
) -> None:
    _use_runtime(monkeypatch, _runtime(tmp_path))
    done = gx.run_git(["fetch", "origin"], capture_output=True, text=True, check=True)
    assert done.stdout.strip() == "bundled fetch origin"


@posix_only
def test_children_get_the_bundled_git_only_without_a_real_one(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, clean_env: Path
) -> None:
    runtime = _runtime(tmp_path)
    _use_runtime(monkeypatch, runtime)
    git_dir = str((runtime / "git" / gx.BUNDLED_GIT_RELATIVE).parent)
    env = {"PATH": str(clean_env), "GIT_EXEC_PATH": "/another"}
    assert gx.with_bundled_git(env) is env
    assert env["PATH"] == os.pathsep.join([git_dir, str(clean_env)])
    assert "GIT_EXEC_PATH" not in env

    _script(clean_env / "git", "echo mine")
    own = {"PATH": str(clean_env), "GIT_EXEC_PATH": "/mine"}
    assert gx.with_bundled_git(dict(own)) == own


@posix_only
def test_children_behind_the_macos_placeholder_get_the_bundled_git(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    clean_env: Path,
    macos_placeholder: tuple[Path, Path],
) -> None:
    placeholder, marker = macos_placeholder
    runtime = _runtime(tmp_path)
    _use_runtime(monkeypatch, runtime)
    env = {"PATH": str(placeholder.parent)}
    gx.with_bundled_git(env)
    assert env["PATH"].split(os.pathsep)[0] == str(
        (runtime / "git" / gx.BUNDLED_GIT_RELATIVE).parent
    )
    assert not marker.exists()


def test_without_a_bundled_git_children_are_unchanged(clean_env: Path) -> None:
    env = {"PATH": str(clean_env)}
    assert gx.with_bundled_git(dict(env)) == env
