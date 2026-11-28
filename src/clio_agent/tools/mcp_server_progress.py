"""Wait for an MCP server while ITS OWN process tree is working; never a fixed deadline.

Every MCP connect/list wait here measures exactly the awaited server:

* :func:`wait_while_server_works` binds a fresh :class:`ProcessTreeWork` for the wait
  (in a context variable, copied into the awaited task and every task it starts);
* the stdio spawn hook (:func:`install_spawn_tracking`) adds each MCP server process
  spawned inside that wait as a root of the tree;
* the wait samples that tree after each no-progress window: work advancing -> the server
  is starting, keep waiting; flat while the server process is still alive -> a visible
  ``mcp_server_stalled`` report, and the wait goes on with the same process (a cold
  import from a network filesystem can look flat, and a respawn would only start over);
  a flat server while one of this process's provisions is working is a server queued
  behind that build's uv lock, so the build's work counts as its progress;
  no server process alive at all -> :class:`NoProgressTimeout`, typed, unless the server
  is still queued behind a live holder of the shared launcher cache
  (:func:`queued_behind`), whose own liveness rules govern that queue. There is no
  ceiling: whoever waits can stop it (a Stop cancels the wait).

Nothing else is measured: not the clio-core daemon (a psutil child of this process),
not another namespace's server, not a shell or a CLI this process also runs.
"""

from __future__ import annotations

import asyncio
import contextvars
import logging
import time
import weakref
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from typing import Any

from clio_agent.arc.daemon_progress import NO_PROGRESS
from clio_agent.runtime import trace
from clio_agent.runtime.progress import ProcessTreeWork, progressed
from clio_agent.tools.preparation_threads import run_preparation_step

logger = logging.getLogger(__name__)

#: A flat window while the awaited server process is still alive: reported, and waited on.
MCP_SERVER_STALLED = "mcp_server_stalled"

_DEFAULT_NO_PROGRESS_S = 30.0
_DEFAULT_MAX_WAIT_S = 180.0

#: The process tree of the MCP server(s) the current wait is for (``None`` outside one).
_SERVER_TREE: contextvars.ContextVar[ProcessTreeWork | None] = contextvars.ContextVar(
    "clio_mcp_server_tree", default=None
)
_SPAWN_TRACKING_INSTALLED = False
#: Trees bound by :func:`wait_while_server_works`: their connect waits on a live server.
_PATIENT: weakref.WeakSet[ProcessTreeWork] = weakref.WeakSet()
#: Trees whose server has not spawned yet because it queues behind a live holder of a
#: shared resource (:func:`queued_behind`); the count is the number of such queues.
_QUEUED: weakref.WeakKeyDictionary[ProcessTreeWork, int] = weakref.WeakKeyDictionary()

_PROGRESS_WAITS = (
    "Fixed MCP deadlines were replaced by progress-based waits "
    "(tools.mcp.no_progress_s, tools.mcp.max_wait_s)"
)
#: The fixed MCP deadline keys these waits replaced: (key, env, what was removed). A
#: leftover one is a typed ``config_key_removed`` error
#: (:func:`clio_agent.removed_config_keys.reject_removed_config_keys`), never ignored.
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
    """An MCP server answered nothing for a whole window and no process of it is alive
    (it never started, or it exited), or a connect outside a measured wait gave up.

    Attributes:
        reason: ``no_progress`` (``ceiling`` is kept for callers that still type it).
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
    """``tools.mcp.max_wait_s`` / ``CLIO_MCP_MAX_WAIT_S`` (180 s): how long a connect
    outside a measured wait (a proxy's clone, a page read) keeps probing a working server,
    and the launcher cache lock's hold allowance. A measured wait has no ceiling."""
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


def waits_on_live_server() -> bool:
    """Whether the current connect is inside a measured wait whose server is alive.

    Such a connect keeps waiting on that same process while it is silent, instead of
    giving up into a respawn (:mod:`clio_agent.tools.mcp_probe_hardening`).
    """
    tree = _SERVER_TREE.get()
    return tree is not None and tree in _PATIENT and tree.alive()


def current_tree() -> ProcessTreeWork | None:
    """The process tree the current wait measures (``None`` outside one)."""
    return _SERVER_TREE.get()


@contextmanager
def queued_behind(resource: str) -> Iterator[None]:
    """Mark the current wait's server as queued behind ``resource``'s live holder.

    The server cannot spawn until that holder lets go (another server building the
    shared uv launcher cache, :mod:`clio_agent.tools.launcher_cache_lock`), so no process
    of its own is alive yet. That is a queue, not a dead server: the holder's own
    liveness rules govern it, and :func:`wait_while_server_works` keeps waiting.
    """
    tree = _SERVER_TREE.get()
    if tree is None:
        yield
        return
    _QUEUED[tree] = _QUEUED.get(tree, 0) + 1
    logger.info("mcp_server_queued resource=%s", resource)
    try:
        yield
    finally:
        remaining = _QUEUED.get(tree, 1) - 1
        if remaining > 0:
            _QUEUED[tree] = remaining
        else:
            _QUEUED.pop(tree, None)


def follow_tree(other: ProcessTreeWork | None) -> None:
    """Make the current wait also measure ``other``: it joined that wait's start."""
    tree = _SERVER_TREE.get()
    if tree is not None and other is not None:
        tree.follow(other)


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
        command = str(args[0] if args else kwargs.get("command", ""))
        trace.event(
            "TOOLS",
            "mcp_server_spawned pid=%s command=%s measured=%s",
            pid,
            command.rsplit("/", 1)[-1],
            tree is not None,
        )
        return process

    sdk_stdio._create_platform_compatible_process = _tracked_spawn  # type: ignore[assignment]
    _SPAWN_TRACKING_INSTALLED = True


def _provision_work(builds: ProcessTreeWork) -> float | None:
    """Work of this process's running provisions, followed into ``builds``.

    ``None`` while none has run during this wait. A build that ends reads as progress
    (its members' exit), so the server it held up is waited for once more.
    """
    from clio_agent.tools.mcp_prepare import running_provisions  # noqa: PLC0415

    for running in running_provisions():
        builds.follow(running)
    return builds.sample()


async def wait_while_server_works(
    awaitable: Any,
    *,
    op_name: str = "connect",
    slice_s: float | None = None,
    on_stall: Callable[[float, tuple[int, ...]], None] | None = None,
    on_work: Callable[[float], None] | None = None,
) -> Any:
    """Await ``awaitable`` while the MCP server it starts is alive.

    Each ``slice_s`` (default :func:`mcp_no_progress_s`) without an answer is checked
    against the server's own process tree: work advancing keeps waiting, and so does
    work in a provision this process is running (the server queues behind its uv lock,
    :func:`~clio_agent.tools.mcp_prepare.running_provisions`); a flat window
    while a server process is still alive is reported (``mcp_server_stalled``, and
    ``on_stall(waited_s, roots)`` for a visible state) and the wait goes on with that
    same process; a window with no server process alive raises
    :class:`NoProgressTimeout`. The awaited task is cancelled on the way out.
    """
    install_spawn_tracking()
    window = slice_s if slice_s is not None else mcp_no_progress_s()
    tree = ProcessTreeWork()
    _PATIENT.add(tree)
    token = _SERVER_TREE.set(tree)
    try:
        task = asyncio.ensure_future(awaitable)  # copies the context: the tree is bound
    finally:
        _SERVER_TREE.reset(token)
    started = time.monotonic()
    last = tree.sample()
    builds = ProcessTreeWork()
    last_build: float | None = None
    try:
        while True:
            done, _pending = await asyncio.wait({task}, timeout=window)
            if done:
                return task.result()
            waited = time.monotonic() - started
            work = await run_preparation_step(tree.sample)
            if progressed(work, last):
                logger.info(
                    "mcp_server_busy op=%s reason=server_working waited_s=%.0f roots=%s",
                    op_name,
                    waited,
                    tree.roots,
                )
                last = work
                if on_work is not None:
                    on_work(waited)
                continue
            if _QUEUED.get(tree):
                logger.info(
                    "mcp_server_busy op=%s reason=queued_behind_shared_launcher waited_s=%.0f",
                    op_name,
                    waited,
                )
                last = work
                if on_work is not None:
                    on_work(waited)
                continue
            build = await run_preparation_step(_provision_work, builds)
            if progressed(build, last_build):
                # The server's uv run queues behind a build's uv lock: that build is its work.
                logger.info(
                    "mcp_server_busy op=%s reason=provision_in_progress waited_s=%.0f roots=%s",
                    op_name,
                    waited,
                    builds.roots,
                )
                last, last_build = work, build
                if on_work is not None:
                    on_work(waited)
                continue
            if not await run_preparation_step(tree.alive):
                raise NoProgressTimeout(op_name, NO_PROGRESS, waited, window)
            logger.warning(
                "%s op=%s reason=no_answer_no_measured_work waited_s=%.0f roots=%s "
                "action=keep_waiting_same_process",
                MCP_SERVER_STALLED,
                op_name,
                waited,
                tree.roots,
            )
            if on_stall is not None:
                on_stall(waited, tree.roots)
    finally:
        if not task.done():
            task.cancel()


def connection_context() -> contextvars.Context:
    """A fresh context for a connection that outlives the call that opens it.

    It carries only the current wait's server tree, so a server the connection spawns
    still counts as the wait's progress (:func:`install_spawn_tracking`). Opened outside
    a wait, it gets a tree of its own, so its discover probe can still tell a server
    that is starting from one that is stuck.
    """
    tree = _SERVER_TREE.get()
    if tree is None:
        install_spawn_tracking()
        tree = ProcessTreeWork()
    context = contextvars.Context()
    context.run(_SERVER_TREE.set, tree)
    return context


def bind_connection_tree() -> contextvars.Token[ProcessTreeWork | None] | None:
    """Bind a server tree for a client connect that runs outside any wait.

    The stdio spawn and the ``server/discover`` probe both run inside the connect, so
    the probe measures exactly the server this connect started and keeps waiting while
    it works (up to ``tools.mcp.max_wait_s``) instead of spending a fixed budget on a
    slow machine. Inside a wait, its tree already applies and nothing is bound. Pass
    the token to :func:`release_connection_tree` in the same task.
    """
    if _SERVER_TREE.get() is not None:
        return None
    install_spawn_tracking()
    return _SERVER_TREE.set(ProcessTreeWork())


def release_connection_tree(token: contextvars.Token[ProcessTreeWork | None] | None) -> None:
    """Undo :func:`bind_connection_tree`."""
    if token is not None:
        _SERVER_TREE.reset(token)


_CONNECTION_TREES_INSTALLED = False


def install_connection_trees() -> None:
    """Make every MCP client connect measure the server it starts.

    Wraps ``fastmcp.Client.__aenter__``, the one connect seam every client shares (a
    proxy's per-request clone, a plain transport client, an executor namespace), with
    :func:`bind_connection_tree`. The session task it starts copies the binding, so the
    stdio spawn and the ``server/discover`` probe share one tree. Idempotent.
    """
    global _CONNECTION_TREES_INSTALLED
    if _CONNECTION_TREES_INSTALLED:
        return
    from fastmcp.client.client import Client  # noqa: PLC0415

    enter = Client.__aenter__

    async def __aenter__(self: Any) -> Any:  # noqa: N807 - the connect seam itself
        token = bind_connection_tree()
        try:
            return await enter(self)
        finally:
            release_connection_tree(token)

    Client.__aenter__ = __aenter__  # type: ignore[method-assign]
    _CONNECTION_TREES_INSTALLED = True


__all__ = [
    "MCP_SERVER_STALLED",
    "REMOVED_DEADLINE_KEYS",
    "NoProgressTimeout",
    "bind_connection_tree",
    "connection_context",
    "current_tree",
    "follow_tree",
    "install_connection_trees",
    "install_spawn_tracking",
    "mcp_max_wait_s",
    "mcp_no_progress_s",
    "release_connection_tree",
    "server_work",
    "wait_while_server_works",
    "waits_on_live_server",
]
