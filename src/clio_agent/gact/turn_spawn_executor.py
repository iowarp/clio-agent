"""Dedicated per-depth executors for child-agent turns."""

from __future__ import annotations

import concurrent.futures
import threading
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from fastapi import FastAPI

#: The one in-code fallback for ``agent_tasks.max_concurrent``. Both the
#: resolver below and the lazy per-depth pool read it, so a pool built before
#: :func:`install_agent_task_executor` ran cannot pick a different number than
#: the knob documents.
DEFAULT_MAX_CONCURRENT_AGENT_TASKS = 3


def install_agent_task_executor(app: "FastAPI") -> None:
    """Install the dedicated child-forward executor state on ``app``.

    Pools are created lazily per depth so a parent waiting at one depth cannot
    starve the children that it launched at the next depth.
    """

    from clio_agent import conf  # noqa: PLC0415

    cap = conf.resolve(
        "agent_tasks.max_concurrent",
        env="CLIO_MAX_CONCURRENT_AGENT_TASKS",
        default=DEFAULT_MAX_CONCURRENT_AGENT_TASKS,
        cast=conf.as_int,
    )
    cap = max(1, int(cap or DEFAULT_MAX_CONCURRENT_AGENT_TASKS))
    app.state.max_concurrent_agent_tasks = cap
    app.state.agent_task_executors = {}
    app.state.agent_task_executor_lock = threading.Lock()


def agent_task_executor_for_depth(
    app: "FastAPI", depth: int
) -> concurrent.futures.ThreadPoolExecutor:
    """Return the lazily created child-forward pool for ``depth``."""

    depth = max(1, int(depth or 1))
    pools: dict[int, concurrent.futures.ThreadPoolExecutor] = app.state.agent_task_executors
    lock = app.state.agent_task_executor_lock
    with lock:
        pool = pools.get(depth)
        if pool is None:
            cap = getattr(
                app.state, "max_concurrent_agent_tasks", DEFAULT_MAX_CONCURRENT_AGENT_TASKS
            )
            pool = concurrent.futures.ThreadPoolExecutor(
                max_workers=cap, thread_name_prefix=f"clio-agent-task-d{depth}"
            )
            pools[depth] = pool
        return pool


def shutdown_agent_task_executors(app: "FastAPI") -> None:
    """Shut down every lazily created child-forward pool."""

    pools = getattr(app.state, "agent_task_executors", None) or {}
    for pool in list(pools.values()):
        pool.shutdown(wait=False, cancel_futures=True)
