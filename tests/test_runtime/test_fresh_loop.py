"""A coroutine run on its own event loop leaves nothing of that loop behind."""

from __future__ import annotations

import asyncio
import contextvars
import gc
import re
import weakref
from pathlib import Path

import anyio

from clio_agent.runtime.fresh_loop import run_in_new_loop

_HELD: contextvars.ContextVar[object] = contextvars.ContextVar("held")


class _Agent:
    """Stands in for what a turn's context carries (a ReAct loop, its tools, LMs)."""


_LOOPS: list[weakref.ref[asyncio.AbstractEventLoop]] = []


async def _tool_call() -> int:
    # A tool call offloads to a thread through anyio, as MCP and tool code does.
    _LOOPS.append(weakref.ref(asyncio.get_running_loop()))
    _HELD.set(_Agent())
    return await anyio.to_thread.run_sync(lambda: 7)


def _alive(kind: type) -> int:
    gc.collect()
    return sum(1 for o in gc.get_objects() if isinstance(o, kind))


def test_a_run_keeps_no_loop_and_no_context_value_alive() -> None:
    """Live: every tool call's asyncio.run kept its loop, root task and context."""
    agents = _alive(_Agent)
    _LOOPS.clear()

    results = [run_in_new_loop(_tool_call()) for _ in range(5)]

    assert results == [7] * 5
    gc.collect()
    # Only this test's own loops: a whole-process count also saw other tests' loops.
    assert [ref for ref in _LOOPS if ref() is not None] == []
    assert _alive(_Agent) <= agents + 1  # at most the last worker's, until it exits


def test_server_code_runs_coroutines_through_run_in_new_loop() -> None:
    """A bare asyncio.run would bring the per-loop leak back; only the CLI may use it."""
    root = Path(__file__).resolve().parents[2] / "src" / "clio_agent"
    offenders = [
        f"{path.relative_to(root)}:{number}"
        for path in root.rglob("*.py")
        if "ui" not in path.relative_to(root).parts[:1]
        for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1)
        if re.search(r"(?<![\w`])asyncio\.run\(", line) and not line.lstrip().startswith("#")
    ]
    assert offenders == []
