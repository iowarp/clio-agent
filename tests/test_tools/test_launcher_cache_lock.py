"""Fail-fast bounded lock around the shared uvx/uv-run launcher cache (#1232 pt 3).

A wedged/held-too-long lock must FAIL FAST with a typed reason — never block
the caller indefinitely — and feed the same background re-probe path as any
other discovery degrade.
"""

from __future__ import annotations

import threading
import time

import pytest

from clio_agent.errors import LAUNCHER_CACHE_LOCK_TIMEOUT
from clio_agent.tools.launcher_cache_lock import (
    LauncherCacheLockTimeoutError,
    acquire_launcher_cache_lock,
    launcher_cache_lock_timeout_s,
    uses_shared_launcher_cache,
)
from clio_agent.tools.mcp_config import MCPServerSpec


def test_default_timeout_is_a_positive_bound() -> None:
    assert launcher_cache_lock_timeout_s() > 0


def test_timeout_config_override(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("CLIO_MCP_LAUNCHER_CACHE_LOCK_TIMEOUT_S", "42")
    assert launcher_cache_lock_timeout_s() == 42.0


def test_uses_shared_launcher_cache_true_for_plain_stdio_spec() -> None:
    spec = MCPServerSpec(name="s", transport="stdio", command="uvx", args=("weather-mcp",))
    assert uses_shared_launcher_cache(spec) is True


def test_uses_shared_launcher_cache_false_for_explicit_uv_cache_dir() -> None:
    """A declaration with its OWN UV_CACHE_DIR opted out of the shared dir; never lock it."""

    spec = MCPServerSpec(
        name="s", transport="stdio", command="uvx", env={"UV_CACHE_DIR": "/custom/cache"}
    )
    assert uses_shared_launcher_cache(spec) is False


def test_uses_shared_launcher_cache_false_for_http() -> None:
    spec = MCPServerSpec(name="s", transport="http", url="https://example.com/mcp")
    assert uses_shared_launcher_cache(spec) is False


def test_acquire_and_release_succeeds_uncontended() -> None:
    with acquire_launcher_cache_lock("server-a", timeout_s=5.0):
        pass  # no exception


def test_acquire_is_serialized_against_a_concurrent_holder() -> None:
    """Two acquisitions on the SAME lock path never run concurrently (real filelock)."""

    order: list[str] = []
    release = threading.Event()

    def _hold() -> None:
        with acquire_launcher_cache_lock("server-a", timeout_s=5.0):
            order.append("first-acquired")
            release.wait(timeout=5.0)
            order.append("first-released")

    holder = threading.Thread(target=_hold, daemon=True)
    holder.start()
    # Give the holder a moment to actually acquire before we contend.
    deadline = time.time() + 2.0
    while "first-acquired" not in order and time.time() < deadline:
        time.sleep(0.01)
    assert "first-acquired" in order

    release.set()
    with acquire_launcher_cache_lock("server-a", timeout_s=5.0):
        order.append("second-acquired")
    holder.join(timeout=5.0)
    assert order.index("first-released") < order.index("second-acquired")


def test_wedged_lock_fails_fast_typed_not_silent() -> None:
    """SABOTAGE: a held lock that never releases must raise typed within the bound,
    never hang past it (the exact #1186-family failure this module fixes)."""

    release = threading.Event()

    def _hold_forever() -> None:
        with acquire_launcher_cache_lock("server-b", timeout_s=5.0):
            release.wait(timeout=10.0)

    holder = threading.Thread(target=_hold_forever, daemon=True)
    holder.start()
    time.sleep(0.2)  # let the holder actually acquire

    started = time.monotonic()
    with pytest.raises(LauncherCacheLockTimeoutError) as exc_info:
        with acquire_launcher_cache_lock("server-b", timeout_s=0.5):
            pass
    elapsed = time.monotonic() - started
    assert elapsed < 3.0, "acquisition blocked well past its bound -- not fail-fast"
    assert exc_info.value.server_id == "server-b"
    assert LAUNCHER_CACHE_LOCK_TIMEOUT in str(exc_info.value)

    release.set()
    holder.join(timeout=5.0)
