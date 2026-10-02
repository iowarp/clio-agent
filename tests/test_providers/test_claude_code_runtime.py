"""B11/B12/B15: the pinned CLI, quiet environment, and buffer-size constants.

The CLI selection is cached process-wide, so every test resets the cache first
(``reset_runtime_cache_for_tests``)
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


def test_resolve_cli_path_finds_a_real_cli_on_this_machine() -> None:
    """Against the REAL installed SDK (its wheel bundles claude on this platform):
    the selection is a runnable CLI -- installed or bundled -- that reported a version."""
    pytest.importorskip("claude_agent_sdk")
    path = runtime.resolve_cli_path()
    assert path is not None
    assert Path(path).is_file()
    expected_name = "claude.exe" if platform.system() == "Windows" else "claude"
    assert Path(path).name.lower() == expected_name  # PATHEXT may spell it claude.EXE
    info = runtime.claude_code_runtime_info()
    assert info["cli_path"] == path
    assert info["cli_version"]
    assert info["source"] in {"installed", "bundled"}


def test_resolve_cli_path_is_cached(monkeypatch: pytest.MonkeyPatch) -> None:
    """SABOTAGE: skip the cache -> a second call re-probes ``--version`` ->
    the call-count assertion below goes red."""
    pytest.importorskip("claude_agent_sdk")
    calls = {"n": 0}
    real_popen = subprocess.Popen

    def counting_popen(*args: object, **kwargs: object):  # type: ignore[no-untyped-def]
        calls["n"] += 1
        return real_popen(*args, **kwargs)  # type: ignore[call-overload]

    monkeypatch.setattr(subprocess, "Popen", counting_popen)
    first = runtime.resolve_cli_path()
    count_after_first = calls["n"]
    assert count_after_first >= 1
    second = runtime.resolve_cli_path()
    assert second == first
    assert calls["n"] == count_after_first  # no new probe


def test_bundled_path_is_none_for_a_fake_sdk_module(monkeypatch: pytest.MonkeyPatch) -> None:
    """A test's minimal fake ``claude_agent_sdk`` (a bare ``ModuleType``, no
    ``__file__``) must never raise -- it just has no bundled binary."""
    from clio_agent.providers.components.client_binary import bundled_claude_path

    monkeypatch.setitem(sys.modules, "claude_agent_sdk", ModuleType("claude_agent_sdk"))
    assert bundled_claude_path() is None
    monkeypatch.setitem(sys.modules, "claude_agent_sdk", None)
    assert bundled_claude_path() is None
