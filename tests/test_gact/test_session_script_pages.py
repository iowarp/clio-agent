"""A settled child's pages wait for its background scripts; a deleted session's streams
stop (BR23, CT6).

A background script a child started outlives the child and may still drive its pages,
so the child's pages close when its last script ends, not when its task settles.
"""

from __future__ import annotations

from collections.abc import Iterator
from types import SimpleNamespace
from typing import Any

import pytest

from clio_agent.gact import session_processes
from clio_agent.gact.browser import service as browser
from clio_agent.gact.code_mode import kernels


@pytest.fixture
def closed(monkeypatch: pytest.MonkeyPatch) -> Iterator[list[str]]:
    calls: list[str] = []

    def close_session(session_id: str) -> int:
        calls.append(session_id)
        return 1

    monkeypatch.setattr(browser, "close_session", close_session)
    monkeypatch.setattr(session_processes, "_SCRIPTS", {})
    monkeypatch.setattr(session_processes, "_PAGES_AFTER_SCRIPTS", set())
    yield calls


def test_a_settled_childs_pages_close_with_its_last_script(closed: list[str]) -> None:
    """SABOTAGE: close the pages in ``close_child_pages`` whatever runs -> the running
    script loses its pages when the child settles -> red."""
    session_processes.script_started("sess_child", "task_1")
    session_processes.script_started("sess_child", "task_2")
    session_processes.close_child_pages("sess_child")  # the child's task settled
    assert closed == []
    session_processes.script_ended("sess_child", "task_1")
    assert closed == []
    session_processes.script_ended("sess_child", "task_2")
    assert closed == ["sess_child"]
    assert session_processes._SCRIPTS == {} and session_processes._PAGES_AFTER_SCRIPTS == set()


def test_a_script_ending_before_the_child_settles_closes_nothing(closed: list[str]) -> None:
    session_processes.script_started("sess_child", "task_1")
    session_processes.script_ended("sess_child", "task_1")
    assert closed == []
    session_processes.close_child_pages("sess_child")
    assert closed == ["sess_child"]


def test_deleting_a_session_stops_its_streams(
    closed: list[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    """SABOTAGE: leave the streams out of ``close_session_processes`` -> a deleted
    session's streams stay open, held in memory -> red."""
    stopped: list[str] = []
    monkeypatch.setattr(kernels, "close_session", lambda _sid: None)
    streams = SimpleNamespace(stop_session=lambda sid: stopped.append(sid) or 1)
    app: Any = SimpleNamespace(state=SimpleNamespace(streams=streams))
    session_processes.close_session_processes(app, "sess_gone")
    assert stopped == ["sess_gone"] and closed == ["sess_gone"]
