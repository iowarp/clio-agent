"""Fail-fast lock around the shared uvx/uv-run launcher cache (#1232 pt 3).

``mcp_config.py::transport_for`` isolates every clio-spawned ``uvx``/``uv run``
stdio MCP launcher onto ONE dedicated cache dir
(``mcp_config._mcp_uv_cache_dir``) so clio's spawns never race the
developer's ambient uv cache. That isolation does not, by itself, stop
clio's OWN concurrent cold-cache spawns from racing EACH OTHER on that
shared dedicated dir — the exact failure ``transport_for``'s docstring
already documents: four concurrent cold-cache ``uvx`` spawns building the
same ephemeral env archive can truncate ``pyvenv.cfg`` (astral-sh/uv#11694),
dropping the proxy connection and failing every tool-declaring expert. #1232
pt 2 makes this MORE likely (namespaces discover concurrently instead of
serially), so this module is the necessary safety valve.

Every stdio spawn onto the shared dedicated cache acquires a clio-owned file
lock (``filelock.FileLock``) before starting, bounded by a configurable
timeout. A wedged/held-too-long lock is an IMMEDIATE, typed fail-fast
(``LAUNCHER_CACHE_LOCK_TIMEOUT``) — never a silent indefinite stall — and the
caller (``tools/mcp_discovery.py``) feeds it into the SAME background
re-probe/heal path as any other discovery degrade.
"""

from __future__ import annotations

import logging
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

from filelock import FileLock, Timeout

from clio_agent.errors import LAUNCHER_CACHE_LOCK_TIMEOUT

logger = logging.getLogger(__name__)

_DEFAULT_TIMEOUT_S = 15.0
_LOCK_FILENAME = ".clio-launcher.lock"


class LauncherCacheLockTimeoutError(RuntimeError):
    """A stdio MCP launcher could not acquire the shared uv cache lock in time."""

    def __init__(self, server_id: str, timeout_s: float) -> None:
        self.server_id = server_id
        self.timeout_s = timeout_s
        super().__init__(
            f"MCP server {server_id!r}: could not acquire the shared launcher cache "
            f"lock within {timeout_s:g}s (reason={LAUNCHER_CACHE_LOCK_TIMEOUT})"
        )


def launcher_cache_lock_timeout_s() -> float:
    """Bound (seconds) on acquiring the shared uv-launcher cache lock."""

    from clio_agent import conf  # noqa: PLC0415 - avoid import cycle at module load

    return float(
        conf.resolve(
            "tools.mcp.launcher_cache_lock_timeout_s",
            env="CLIO_MCP_LAUNCHER_CACHE_LOCK_TIMEOUT_S",
            default=_DEFAULT_TIMEOUT_S,
            cast=conf.as_float,
        )
    )


def _lock_path() -> Path:
    from clio_agent.tools.mcp_config import _mcp_uv_cache_dir  # noqa: PLC0415

    cache_dir = _mcp_uv_cache_dir()
    cache_dir.mkdir(parents=True, exist_ok=True)
    return cache_dir / _LOCK_FILENAME


def uses_shared_launcher_cache(spec: object) -> bool:
    """True when ``spec`` (an :class:`MCPServerSpec`) spawns onto the SHARED
    dedicated uv cache — the only spawns this lock needs to serialize.

    Mirrors ``transport_for``'s own condition exactly (``"UV_CACHE_DIR" not in
    spec.env``): a declaration with its own explicit ``UV_CACHE_DIR`` opted out
    of the shared dir, so it cannot race another spawn ON it.
    """

    return (
        getattr(spec, "transport", "") == "stdio"
        and bool(getattr(spec, "command", ""))
        and "UV_CACHE_DIR" not in (getattr(spec, "env", None) or {})
    )


@contextmanager
def acquire_launcher_cache_lock(
    server_id: str, *, timeout_s: float | None = None
) -> Iterator[None]:
    """Bound acquisition of the shared uv-launcher cache lock (#1232 pt 3).

    Raises :class:`LauncherCacheLockTimeoutError` instead of blocking forever
    when the lock is wedged/held too long. Callers treat that exactly like any
    other discovery degrade (immediate, typed, background-re-probed) — see
    ``tools/mcp_discovery.py::_list_one_namespace``.
    """

    bound = timeout_s if timeout_s is not None else launcher_cache_lock_timeout_s()
    lock = FileLock(str(_lock_path()), timeout=bound)
    try:
        with lock:
            yield
    except Timeout as exc:
        logger.warning(
            "launcher_cache_lock_timeout reason=%s server=%s timeout_s=%.1f",
            LAUNCHER_CACHE_LOCK_TIMEOUT,
            server_id,
            bound,
        )
        from clio_agent.runtime.stream_audit import stream_audit  # noqa: PLC0415

        stream_audit(
            "launcher_cache_lock_timeout",
            reason=LAUNCHER_CACHE_LOCK_TIMEOUT,
            server_id=server_id,
            timeout_s=bound,
        )
        raise LauncherCacheLockTimeoutError(server_id, bound) from exc


__all__ = [
    "LauncherCacheLockTimeoutError",
    "acquire_launcher_cache_lock",
    "launcher_cache_lock_timeout_s",
    "uses_shared_launcher_cache",
]
