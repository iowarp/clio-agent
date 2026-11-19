"""The organization-branches note: a fact once a turn reaches several NDP organizations."""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest

from clio_agent import injections
from clio_agent.gact.agents import clio_react
from clio_agent.gact.storage import organization_branches as branches


class _Recorder:
    def __init__(self) -> None:
        self.notes: list[tuple[str, str]] = []

    def injections(self, items: list[tuple[str, str]]) -> None:
        self.notes.extend(items)


def _loop(*tools: str) -> Any:
    return SimpleNamespace(
        agent=SimpleNamespace(tools={name: object() for name in tools}), recorder=_Recorder()
    )


@pytest.fixture
def running(monkeypatch: pytest.MonkeyPatch) -> Any:
    loop = _loop("connected_data_search_ndp", "spawn_agents_parallel")
    monkeypatch.setattr(clio_react, "active_loop", lambda: loop)
    return loop


def test_the_third_organization_notes_branches_once(running: Any) -> None:
    """Repeats and case do not count twice; the note names them and the spawn tool, once.
    SABOTAGE: drop the ``_clio_branches_noted`` check and a second note appears."""
    for name in ("trpa", "TRPA", "cal_fire", "", "cal_fire"):
        branches.note_organization(name)
    assert running.recorder.notes == []
    branches.note_organization("nifc")
    [(source, text)] = running.recorder.notes
    assert source == branches.ORGANIZATION_BRANCHES_SOURCE
    assert "3 organizations (trpa, cal_fire, nifc)" in text and "`spawn_agents_parallel`" in text
    branches.note_organization("usgs")
    assert len(running.recorder.notes) == 1


def test_no_note_without_a_spawn_tool_or_with_the_switch_off(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    loop = _loop("connected_data_search_ndp")
    monkeypatch.setattr(clio_react, "active_loop", lambda: loop)
    for name in ("a", "b", "c", "d"):
        branches.note_organization(name)
    assert loop.recorder.notes == []
    injections.update(branches.ORGANIZATION_BRANCHES_SOURCE, active=False)
    loop = _loop("spawn_agent_task")
    monkeypatch.setattr(clio_react, "active_loop", lambda: loop)
    for name in ("a", "b", "c"):
        branches.note_organization(name)
    assert loop.recorder.notes == []


def test_outside_a_turn_nothing_happens(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(clio_react, "active_loop", lambda: None)
    branches.note_organization("trpa")  # no loop: no error, no note
