"""Wait for an MCP server while ITS OWN process tree is working; never a fixed deadline.

A server still starting on a slow machine (``uv`` installing its environment, Python
importing) answers late; a hung one never answers. Only the awaited server's own work
tells them apart, so every MCP connect/list wait here measures exactly that server:

* :func:`wait_while_server_works` binds a fresh :class:`ProcessTreeWork` for the wait
  (in a context variable, copied into the awaited task and every task it starts);
* the stdio spawn hook (:func:`install_spawn_tracking`) adds each MCP server process
  spawned inside that wait as a root of the tree;
* the wait samples that tree after each no-progress window: work advancing -> the server
  is starting, keep waiting (up to ``tools.mcp.max_wait_s``); flat, or no server process
  at all -> :class:`NoProgressTimeout`, typed.

Nothing else is measured: not the clio-core daemon (a psutil child of this process),
not another namespace's server, not a shell or a CLI this process also runs.
"""

from __future__ import annotations

import asyncio
import contextvars
import logging
import time
from typing import Any

from clio_agent.arc.daemon_progress import CEILING, NO_PROGRESS, ProcessTreeWork

logger = logging.getLogger(__name__)

_MIN_WORK_PROGRESS = 0.01
_DEFAULT_NO_PROGRESS_S = 30.0
_DEFAULT_MAX_WAIT_S = 180.0

#: The process tree of the MCP server(s) the current wait is for (``None`` outside one).
_SERVER_TREE: contextvars.ContextVar[ProcessTreeWork | None] = contextvars.ContextVar(
    "clio_mcp_server_tree", default=None
)
_SPAWN_TRACKING_INSTALLED = False

_PROGRESS_WAITS = (
    "Fixed MCP deadlines were replaced by progress-based waits "
    "(tools.mcp.no_progress_s, tools.mcp.max_wait_s)"
)
#: The fixed MCP deadline keys these waits replaced: (key, env, what was removed). A
#: leftover one is a typed ``config_key_removed`` error
#: (:func:`clio_agent.config.reject_removed_config_keys`), never ignored.
REMOVED_DEADLINE_KEYS: tuple[tuple[str, str, str], ...] = (
    ("tools.mcp.setup_timeout_s", "CLIO_MCP_SETUP_TIMEOUT_S", _PROGRESS_WAITS),
    ("tools.mcp.cold_spawn_runaway_s", "CLIO_MCP_COLD_SPAWN_RUNAWAY_S", _PROGRESS_WAITS),
    (
        "tools.mcp.launcher_cache_lock_timeout_s",
        "CLIO_MCP_LAUNCHER_CACHE_LOCK_TIMEOUT_S",
        _PROGRESS_WAITS,
    ),
    ("limits.mcp_reconnect_timeout_s", "CLIO_GACT_MCP_RECONNECT_TIMEOUT_S", _PROGRESS_WAITS),
)


class NoProgressTimeout(TimeoutError):
    """An MCP server answered nothing and its own process tree did no work for a whole
    window, or it was still working at the ``tools.mcp.max_wait_s`` ceiling.

    Attributes:
        reason: ``no_progress`` or ``ceiling``.
        waited_s: Seconds waited.
        op_name: What was awaited (``connect`` / ``list`` / ``reconnect`` ...).
    """

    def __init__(self, op_name: str, reason: str, waited_s: float, window_s: float) -> None:
        self.op_name = op_name
        self.reason = reason
        self.waited_s = waited_s
        what = (
            f"no answer and no server progress for {window_s:g}s"
            if reason == NO_PROGRESS
            else "still working at the tools.mcp.max_wait_s ceiling"
        )
        super().__init__(f"MCP {op_name}: {what} (waited {waited_s:.0f}s, reason=mcp_{reason})")


def mcp_max_wait_s() -> float:
    """``tools.mcp.max_wait_s`` / ``CLIO_MCP_MAX_WAIT_S`` (180 s): the ceiling on waiting for
    a server that is still visibly starting (its process tree working)."""
    from clio_agent import conf  # noqa: PLC0415

    value = conf.resolve(
        "tools.mcp.max_wait_s",
        env="CLIO_MCP_MAX_WAIT_S",
        default=_DEFAULT_MAX_WAIT_S,
        cast=conf.as_float,
    )
    return value if value > 0 else _DEFAULT_MAX_WAIT_S


def mcp_no_progress_s() -> float:
    """``tools.mcp.no_progress_s`` / ``CLIO_MCP_NO_PROGRESS_S`` (30 s): how long an MCP
    connect may go with no answer AND no work in the server's own process tree."""
    from clio_agent import conf  # noqa: PLC0415

    value = conf.resolve(
        "tools.mcp.no_progress_s",
        env="CLIO_MCP_NO_PROGRESS_S",
        default=_DEFAULT_NO_PROGRESS_S,
        cast=conf.as_float,
    )
    return value if value > 0 else _DEFAULT_NO_PROGRESS_S


def server_work() -> float | None:
    """The current wait's server tree work; ``None`` outside a wait or before a spawn."""
    tree = _SERVER_TREE.get()
    return tree.sample() if tree is not None else None


def install_spawn_tracking() -> None:
    """Record every stdio MCP server spawned inside a wait as a root of that wait's tree.

    Wraps ``mcp.client.stdio._create_platform_compatible_process`` (the one spawn point
    of the stdio transport, looked up at call time). Idempotent.
    """
    global _SPAWN_TRACKING_INSTALLED
    if _SPAWN_TRACKING_INSTALLED:
        return
    import mcp.client.stdio as sdk_stdio  # noqa: PLC0415

    spawn = sdk_stdio._create_platform_compatible_process

    async def _tracked_spawn(*args: Any, **kwargs: Any) -> Any:
        process = await spawn(*args, **kwargs)
        tree = _SERVER_TREE.get()
        pid = getattr(process, "pid", None)
        if tree is not None and isinstance(pid, int):
            tree.add_root(pid)
        return process

    sdk_stdio._create_platform_compatible_process = _tracked_spawn  # type: ignore[assignment]
    _SPAWN_TRACKING_INSTALLED = True


async def wait_while_server_works(
    awaitable: Any, *, op_name: str = "connect", slice_s: float | None = None
) -> Any:
    """Await ``awaitable`` while the MCP server it starts keeps working.

    Each ``slice_s`` (default :func:`mcp_no_progress_s`) without an answer is checked
    against the server's own process tree: work advancing keeps waiting, up to
    :func:`mcp_max_wait_s`; a window with no progress (or no server process at all)
    raises :class:`NoProgressTimeout`. The awaited task is cancelled on the way out.
    """
    install_spawn_tracking()
    window = slice_s if slice_s is not None else mcp_no_progress_s()
    ceiling = mcp_max_wait_s()
    tree = ProcessTreeWork()
    token = _SERVER_TREE.set(tree)
    try:
        task = asyncio.ensure_future(awaitable)  # copies the context: the tree is bound
    finally:
        _SERVER_TREE.reset(token)
    started = time.monotonic()
    last = tree.sample()
    try:
        while True:
            done, _pending = await asyncio.wait({task}, timeout=window)
            if done:
                return task.result()
            waited = time.monotonic() - started
            work = await asyncio.to_thread(tree.sample)
            if work is None or (last is not None and work - last < _MIN_WORK_PROGRESS):
                raise NoProgressTimeout(op_name, NO_PROGRESS, waited, window)
            if waited >= ceiling:
                raise NoProgressTimeout(op_name, CEILING, waited, window)
            logger.info(
                "mcp_server_busy op=%s reason=server_working waited_s=%.0f roots=%s",
                op_name,
                waited,
                tree.roots,
            )
            last = work
    finally:
        if not task.done():
            task.cancel()


__all__ = [
    "REMOVED_DEADLINE_KEYS",
    "NoProgressTimeout",
    "install_spawn_tracking",
    "mcp_max_wait_s",
    "mcp_no_progress_s",
    "server_work",
    "wait_while_server_works",
]
