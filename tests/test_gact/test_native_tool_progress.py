"""A native tool's document job shows that it is still running and stops with the turn."""

from __future__ import annotations

import sys
import threading
from pathlib import Path
from typing import Any

import pytest

from clio_agent.gact.agents import tool_progress
from clio_agent.runtime.cancellation import cancellation_checker
from clio_agent.runtime.document_stack import process as document_process
from clio_agent.tools import execution


@pytest.fixture
def rows(monkeypatch: pytest.MonkeyPatch) -> list[tuple[str, str, dict[str, Any]]]:
    seen: list[tuple[str, str, dict[str, Any]]] = []

    def observe(name: str, _args: Any, phase: str, *, result: Any = None, **_kw: Any) -> None:
        seen.append((name, phase, result))

    monkeypatch.setattr(execution, "notify_global_tool_observer", observe)
    monkeypatch.setattr(document_process, "HEARTBEAT_FIRST_S", 0.2)
    monkeypatch.setattr(document_process, "HEARTBEAT_EVERY_S", 0.2)
    return seen


def test_progress_outside_a_tool_is_not_shown(rows: list[Any]) -> None:
    assert tool_progress.report_tool_progress("hello") is False
    assert rows == []


def test_a_long_job_reports_on_its_row_and_finishes(rows: list[Any]) -> None:
    """SABOTAGE: drop the heartbeat from ``_running_tool`` -> no progress rows -> red."""
    with tool_progress._running_tool("document_convert", {}, "handle-1"):
        out = document_process.run(
            [sys.executable, "-c", "import time; time.sleep(0.8); print('ok')"], cwd=Path.cwd()
        )
    assert out.strip() == "ok"
    progress = [r for r in rows if r[1] == "progress"]
    assert progress, "the row must show the job is still running"
    name, _, result = progress[0]
    assert name == "document_convert" and result["observer_handle"] == "handle-1"
    assert result["message"].startswith("Still running after")
    assert "Stop the turn to cancel" in result["message"]


def test_stopping_the_turn_ends_the_job(rows: list[Any]) -> None:
    """SABOTAGE: drop ``cancellation_requested`` from the scope -> the 30 s job runs on."""
    stop = threading.Event()
    threading.Timer(0.4, stop.set).start()
    with (
        cancellation_checker(stop.is_set),
        tool_progress._running_tool("document_convert", {}, "h"),
        pytest.raises(document_process.DocumentCancelled),
    ):
        document_process.run([sys.executable, "-c", "import time; time.sleep(30)"], cwd=Path.cwd())
