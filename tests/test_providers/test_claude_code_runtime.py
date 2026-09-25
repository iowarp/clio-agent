"""B11/B12/B15: the pinned CLI, quiet environment, and buffer-size constants.

``resolve_cli_path`` / ``claude_cli_version`` are cached process-wide (module
globals), so every test resets the cache first (``reset_runtime_cache_for_tests``)
-- a value cached by an EARLIER test (fake or real SDK) must never leak into
a LATER test's assertion.
"""

from __future__ import annotations

import platform
import subprocess
import sys
from pathlib import Path
from types import ModuleType

import pytest

from clio_agent.providers import claude_code_runtime as runtime


@pytest.fixture(autouse=True)
def _clean_cache() -> None:
    runtime.reset_runtime_cache_for_tests()
    yield
    runtime.reset_runtime_cache_for_tests()


def test_quiet_env_disables_telemetry_and_autoupdate() -> None:
    assert runtime.QUIET_ENV == {
        "CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC": "1",
        "DISABLE_AUTOUPDATER": "1",
    }


def test_max_buffer_size_is_generously_above_the_sdk_default() -> None:
    """SABOTAGE: leave the SDK's tight 1 MiB default in place -> a long assistant
    turn trips the buffer -> this floor check goes red."""
    one_mib = 1024 * 1024
    assert runtime.MAX_BUFFER_SIZE > one_mib
    assert runtime.MAX_BUFFER_SIZE == 16 * 1024 * 1024


def test_resolve_cli_path_finds_the_real_bundled_binary() -> None:
    """Against the REAL installed SDK on this dev machine (the claude-code
    extra's wheel bundles claude.exe on this platform)."""
    pytest.importorskip("claude_agent_sdk")
    path = runtime.resolve_cli_path()
    assert path is not None
    assert Path(path).is_file()
    expected_name = "claude.exe" if platform.system() == "Windows" else "claude"
    assert Path(path).name == expected_name


def test_resolve_cli_path_is_cached(monkeypatch: pytest.MonkeyPatch) -> None:
    """SABOTAGE: skip the cache -> a second call re-probes the filesystem ->
    the call-count assertion below goes red."""
    calls = {"n": 0}
    real_is_file = Path.is_file

    def counting_is_file(self: Path) -> bool:
        calls["n"] += 1
        return real_is_file(self)

    monkeypatch.setattr(Path, "is_file", counting_is_file)
    pytest.importorskip("claude_agent_sdk")

    first = runtime.resolve_cli_path()
    count_after_first = calls["n"]
    second = runtime.resolve_cli_path()

    assert second == first
    assert calls["n"] == count_after_first  # no new filesystem probe


def test_resolve_cli_path_returns_none_for_a_fake_sdk_module(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A test's minimal fake ``claude_agent_sdk`` (a bare ``ModuleType``, no
    ``__file__``) must never raise -- just report no bundled binary to pin."""
    fake = ModuleType("claude_agent_sdk")
    monkeypatch.setitem(sys.modules, "claude_agent_sdk", fake)
    assert runtime.resolve_cli_path() is None


def test_resolve_cli_path_returns_none_when_sdk_is_absent(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setitem(sys.modules, "claude_agent_sdk", None)
    assert runtime.resolve_cli_path() is None


def test_claude_cli_version_probes_the_real_binary() -> None:
    pytest.importorskip("claude_agent_sdk")
    path = runtime.resolve_cli_path()
    assert path is not None
    version = runtime.claude_cli_version(path)
    assert version  # non-empty: the real bundled claude.exe answers --version


def test_claude_cli_version_is_cached_per_path(monkeypatch: pytest.MonkeyPatch) -> None:
    calls = {"n": 0}
    real_run = subprocess.run

    def counting_run(*args: object, **kwargs: object):  # type: ignore[no-untyped-def]
        calls["n"] += 1
        return real_run(*args, **kwargs)

    monkeypatch.setattr(subprocess, "run", counting_run)
    pytest.importorskip("claude_agent_sdk")
    path = runtime.resolve_cli_path()
    assert path is not None

    runtime.claude_cli_version(path)
    assert calls["n"] == 1
    runtime.claude_cli_version(path)
    assert calls["n"] == 1  # cached -- no second probe


def test_claude_cli_version_returns_empty_for_none_path() -> None:
    assert runtime.claude_cli_version(None) == ""


def test_claude_cli_version_returns_empty_and_never_raises_on_a_missing_binary() -> None:
    assert runtime.claude_cli_version("C:/definitely/not/a/real/claude.exe") == ""


def test_claude_cli_version_returns_empty_on_a_nonzero_exit(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def fake_run(*args: object, **kwargs: object) -> subprocess.CompletedProcess:
        return subprocess.CompletedProcess(args=args, returncode=1, stdout="", stderr="boom")

    monkeypatch.setattr(subprocess, "run", fake_run)
    assert runtime.claude_cli_version("some/path") == ""


def test_claude_code_runtime_info_reports_both_fields() -> None:
    pytest.importorskip("claude_agent_sdk")
    info = runtime.claude_code_runtime_info()
    assert set(info) == {"cli_path", "cli_version"}
    assert info["cli_path"]
    assert info["cli_version"]
