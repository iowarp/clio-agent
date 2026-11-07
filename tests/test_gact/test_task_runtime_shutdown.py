"""A clean shutdown retires every background effect even when one fails (taskable CT25).

Deferred file versions, a script's queued notifies and open streams are written at
shutdown; a failure of one never skips the next, nor the task runtime's other resources,
and is reported with the cleanup errors.
"""

from __future__ import annotations

import asyncio
import threading
import time
from types import SimpleNamespace
from typing import Any

import pytest

from clio_agent.gact import code_task_reporting, task_runtime
from clio_agent.gact.artifacts import version_pacing
from clio_agent.gact.code_task_context import _BACKGROUND_TASK, BackgroundTask


class _Closing:
    def __init__(self) -> None:
        self.closed = False

    async def close(self) -> None:
        self.closed = True


class _FailingStreams:
    def __init__(self) -> None:
        self.asked = False

    def close(self) -> None:
        self.asked = True
        raise ExceptionGroup("streams failed to stop at shutdown", [OSError("disk full")])


def test_a_failing_retirement_skips_neither_the_next_nor_the_other_resources(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """SABOTAGE: call the retirements without the per-step guard -> the notify flush's
    failure skips the streams and the question events -> red."""
    sent: list[Any] = []

    def flush_all(app: Any) -> int:
        sent.append(app)
        raise RuntimeError("history unavailable")

    monkeypatch.setattr(code_task_reporting, "flush_all", flush_all)
    streams, questions = _FailingStreams(), _Closing()
    app = SimpleNamespace(state=SimpleNamespace(streams=streams, question_events=questions))
    errors = asyncio.run(task_runtime.close(app))
    assert sent == [app] and streams.asked and questions.closed
    assert [type(e).__name__ for e in errors] == ["RuntimeError", "ExceptionGroup"]


def test_a_deferred_version_that_fails_is_traced(monkeypatch: pytest.MonkeyPatch) -> None:
    """SABOTAGE: log it only (``logger.warning``) -> no trace event -> red."""
    traced: list[tuple[Any, ...]] = []
    monkeypatch.setattr(version_pacing.trace, "event", lambda *args: traced.append(args))
    monkeypatch.setenv("CLIO_ARTIFACTS_VERSION_INTERVAL_S", "60")
    monkeypatch.setattr(version_pacing, "_NAMES", {})

    def fail() -> None:
        raise OSError("disk full")

    token = _BACKGROUND_TASK.set(BackgroundTask("task_1", threading.Event()))
    try:
        assert version_pacing.pace("s1", "ws", "a.csv", lambda: None) is None
        assert version_pacing.pace("s1", "ws", "a.csv", fail)
    finally:
        _BACKGROUND_TASK.reset(token)
    assert version_pacing.flush_pending() == 1
    assert traced and version_pacing.VERSION_DEFERRED_FAILED in traced[0]


def test_names_paced_an_interval_ago_are_forgotten(monkeypatch: pytest.MonkeyPatch) -> None:
    """SABOTAGE: skip ``_prune`` -> one entry per file a script ever registered -> red."""
    monkeypatch.setenv("CLIO_ARTIFACTS_VERSION_INTERVAL_S", "0.05")
    monkeypatch.setattr(version_pacing, "_NAMES", {})
    token = _BACKGROUND_TASK.set(BackgroundTask("task_1", threading.Event()))
    try:
        for name in ("a.csv", "b.csv", "c.csv"):
            assert version_pacing.pace("s1", "ws", name, lambda: None) is None
        assert len(version_pacing._NAMES) == 3
        time.sleep(0.1)
        assert version_pacing.pace("s1", "ws", "d.csv", lambda: None) is None
    finally:
        _BACKGROUND_TASK.reset(token)
    assert list(version_pacing._NAMES) == [("s1", "ws", "d.csv")]
