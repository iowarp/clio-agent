"""How many tool calls run at once, for a step's calls and a script's calls alike.

A step may hold any number of calls, and a ``run_code`` script may gather any number.
Running them all at once gave each its own thread; with a thousand ``get_record``
calls in one step, the threads' cancellation polls and the MCP replies they waited on
contended for the same locks and event loop until the server stopped answering
(base P7, 2026-11-01). Both paths therefore share one bound: at most
``tools.per_server_concurrency`` calls to one server at a time, from a pool of
:data:`WORKERS` threads. Calls that wait for a slot keep their order; outcomes are
returned in call order.
"""

from __future__ import annotations

import contextvars
import threading
from collections import defaultdict
from collections.abc import Callable, Iterable, Sequence
from concurrent.futures import ThreadPoolExecutor
from typing import TypeVar

from clio_agent import conf

__all__ = ["WORKERS", "ServerGates", "per_server_concurrency", "run_bounded"]

#: Threads one step or one script runs its calls on.
WORKERS = 32

_C = TypeVar("_C")
_R = TypeVar("_R")


def per_server_concurrency() -> int:
    """Calls to one server that may be running at once (at least 1)."""
    value = conf.resolve(
        "tools.per_server_concurrency",
        env="CLIO_TOOLS_PER_SERVER_CONCURRENCY",
        default=8,
        cast=conf.as_int,
    )
    return max(1, value)


class ServerGates:
    """One semaphore per server, sized by :func:`per_server_concurrency`."""

    def __init__(self) -> None:
        limit = per_server_concurrency()
        self._gates: defaultdict[str, threading.Semaphore] = defaultdict(
            lambda: threading.Semaphore(limit)
        )
        self._lock = threading.Lock()

    def __call__(self, server: str) -> threading.Semaphore:
        with self._lock:
            return self._gates[server]


def server_of(tool: str, servers: Iterable[str]) -> str:
    """The server a tool belongs to, or the tool's own name for a native tool."""
    for server in sorted(servers, key=len, reverse=True):
        if tool.startswith(f"{server}_"):
            return server
    return tool


def run_bounded(
    calls: Sequence[_C],
    run: Callable[[_C], _R],
    tool_name: Callable[[_C], str],
    servers: Iterable[str],
    *,
    thread_name_prefix: str,
) -> list[_R]:
    """Run ``calls`` concurrently within the bounds; outcomes in call order.

    Each call runs in a copy of the caller's context, as a direct call would.
    """
    servers = tuple(servers)
    gates = ServerGates()

    def gated(call: _C) -> _R:
        with gates(server_of(tool_name(call), servers)):
            return run(call)

    workers = min(len(calls), WORKERS)
    with ThreadPoolExecutor(max_workers=workers, thread_name_prefix=thread_name_prefix) as pool:
        futures = [pool.submit(contextvars.copy_context().run, gated, call) for call in calls]
        return [future.result() for future in futures]
