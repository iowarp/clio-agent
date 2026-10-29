"""A coroutine run on its own event loop leaves nothing of that loop behind."""

from __future__ import annotations

import asyncio
import contextvars
import gc
import re
import threading
import weakref
from pathlib import Path

import anyio

from clio_agent.runtime.fresh_loop import run_in_new_loop

_HELD: contextvars.ContextVar[object] = contextvars.ContextVar("held")


class _Agent:
    """Stands in for what a turn's context carries (a ReAct loop, its tools, LMs)."""


_LOOPS: list[weakref.ref[asyncio.AbstractEventLoop]] = []
_WORKERS: list[weakref.ref[threading.Thread]] = []
_ROOT_TASK: anyio.lowlevel.RunVar[asyncio.Task[object] | None] = anyio.lowlevel.RunVar(
    "test_retained_root_task"
)


def _offloaded_result() -> int:
    """Record this test's real worker without keeping its root task alive."""
    _WORKERS.append(weakref.ref(threading.current_thread()))
    return 7


async def _tool_call() -> int:
    # A tool call offloads to a thread through anyio, as MCP and tool code does.
    _LOOPS.append(weakref.ref(asyncio.get_running_loop()))
    _HELD.set(_Agent())
    # Reproduce loop-bound RunVar state even on AnyIO versions that now clear
    # their own root-task entry. Our wrapper must release all per-loop state.
    _ROOT_TASK.set(asyncio.current_task())
    return await anyio.to_thread.run_sync(_offloaded_result)


def _join_owned_workers() -> None:
    """Wait for the stop callbacks already issued when each root task finished."""
    for ref in _WORKERS:
        worker = ref()
        if worker is not None:
            worker.join(timeout=5)
            assert not worker.is_alive(), "The completed run left its worker running"


def _alive(kind: type) -> int:
    gc.collect()
    return sum(1 for o in gc.get_objects() if isinstance(o, kind))


def test_a_run_keeps_no_loop_and_no_context_value_alive() -> None:
    """Live: every tool call's asyncio.run kept its loop, root task and context."""
    agents = _alive(_Agent)
    _LOOPS.clear()
    _WORKERS.clear()

    results = [run_in_new_loop(_tool_call()) for _ in range(5)]

    assert results == [7] * 5
    assert len(_WORKERS) == 5
    # AnyIO queues a stop when the root task finishes. A worker still exiting
    # legitimately holds its root task and loop; settle only our own workers
    # before checking whether the completed runs retain anything.
    _join_owned_workers()
    gc.collect()
    # Only this test's own loops: a whole-process count also saw other tests' loops.
    assert [ref for ref in _LOOPS if ref() is not None] == []
    assert _alive(_Agent) <= agents


def test_server_code_runs_coroutines_through_run_in_new_loop() -> None:
    """A bare asyncio.run would bring the per-loop leak back; only the CLI may use it."""
    root = Path(__file__).resolve().parents[2] / "src" / "clio_agent"
    offenders = [
        f"{path.relative_to(root)}:{number}"
        for path in root.rglob("*.py")
        if "ui" not in path.relative_to(root).parts[:1]
        if path.relative_to(root).as_posix() != "gact/store_migrate_command.py"
        for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1)
        if re.search(r"(?<![\w`])asyncio\.run\(", line) and not line.lstrip().startswith("#")
    ]
    assert offenders == []
