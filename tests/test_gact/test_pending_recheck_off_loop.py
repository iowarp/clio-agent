"""The pending-mail recheck reads Core off the server loop and signals on it."""

from __future__ import annotations

import asyncio
import threading
from types import SimpleNamespace
from typing import Any

import pytest

from clio_agent.gact.conversation_recovery import (
    PENDING_RECHECK_FAILED,
    signal_pending_sessions,
)
from clio_agent.gact.core_io import CoreIO


def _owner(pending: Any) -> tuple[Any, list[tuple[str, str]], list[str]]:
    signals: list[tuple[str, str]] = []
    reads: list[str] = []

    def pending_sessions() -> tuple[str, ...]:
        reads.append(threading.current_thread().name)
        return pending()

    owner = SimpleNamespace(
        _closing=False,
        history=SimpleNamespace(io=CoreIO(), pending_sessions=pending_sessions),
        runner=SimpleNamespace(
            signal=lambda sid: signals.append((sid, threading.current_thread().name))
        ),
    )
    return owner, signals, reads


def test_the_index_is_read_off_the_loop_and_signals_land_on_it() -> None:
    owner, signals, reads = _owner(lambda: ("a", "b"))

    async def scenario() -> str:
        signal_pending_sessions(owner, asyncio.get_running_loop())  # type: ignore[arg-type]
        for _ in range(200):
            if len(signals) == 2:
                break
            await asyncio.sleep(0.01)
        return threading.current_thread().name

    loop_thread = asyncio.run(scenario())
    assert reads and reads[0] != loop_thread
    assert signals == [("a", loop_thread), ("b", loop_thread)]


def test_a_failed_read_is_logged_typed(caplog: pytest.LogCaptureFixture) -> None:
    def broken() -> tuple[str, ...]:
        raise OSError("clio-core unavailable")

    owner, signals, _reads = _owner(broken)

    async def scenario() -> None:
        signal_pending_sessions(owner, asyncio.get_running_loop())  # type: ignore[arg-type]
        for _ in range(200):
            if PENDING_RECHECK_FAILED in caplog.text:
                break
            await asyncio.sleep(0.01)

    asyncio.run(scenario())
    assert PENDING_RECHECK_FAILED in caplog.text and signals == []
