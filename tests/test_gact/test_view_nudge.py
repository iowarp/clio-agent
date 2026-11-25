"""The view note: a fact after a run of calls with nothing shown, never a drawing."""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest

from clio_agent import injections
from clio_agent.gact import view_nudge


class _Recorder:
    def __init__(self) -> None:
        self.notes: list[tuple[str, str]] = []

    def injections(self, items: list[tuple[str, str]]) -> None:
        self.notes.extend(items)


def _loop(*, root: bool = True, views: bool = True) -> Any:
    tools = {"shell_bash": object()}
    if views:
        tools |= {"create_a2ui_surface": object(), "capture_a2ui_surface": object()}
    loop = SimpleNamespace(agent=SimpleNamespace(tools=tools), recorder=_Recorder())
    if not root:
        loop._clio_view_nudge_applies = False
    elif views:
        loop._clio_view_nudge_applies = True
    return loop


@pytest.fixture(autouse=True)
def threshold() -> None:
    injections.update(view_nudge.VIEW_NUDGE_SOURCE, params={"after": 4})


def test_after_a_run_of_calls_with_nothing_shown_the_loop_notes_it_once() -> None:
    """SABOTAGE: drop the ``_clio_view_noted`` check and a second note appears."""
    loop = _loop()
    view_nudge.note_call_run(loop, ["shell_bash", "web_search"])
    assert loop.recorder.notes == []
    view_nudge.note_call_run(loop, ["shell_bash", "shell_bash"])
    [(source, text)] = loop.recorder.notes
    assert source == view_nudge.VIEW_NUDGE_SOURCE
    assert "4 calls" in text and "create_a2ui_surface" in text and "carry on" in text
    view_nudge.note_call_run(loop, ["shell_bash"] * 9)
    assert len(loop.recorder.notes) == 1


def test_a_view_call_restarts_the_count() -> None:
    loop = _loop()
    view_nudge.note_call_run(loop, ["shell_bash"] * 3)
    view_nudge.note_call_run(loop, ["create_a2ui_surface"])
    view_nudge.note_call_run(loop, ["shell_bash"] * 3)
    assert loop.recorder.notes == []
    view_nudge.note_call_run(loop, ["capture_a2ui_surface", "shell_bash"])
    assert loop.recorder.notes == []


def test_children_and_agents_without_views_are_never_told() -> None:
    for loop in (_loop(root=False), _loop(views=False)):
        view_nudge.note_call_run(loop, ["shell_bash"] * 8)
        assert loop.recorder.notes == []


def test_the_switch_and_a_zero_threshold_turn_it_off() -> None:
    injections.update(view_nudge.VIEW_NUDGE_SOURCE, params={"after": 0})
    loop = _loop()
    view_nudge.note_call_run(loop, ["shell_bash"] * 8)
    assert loop.recorder.notes == []
    injections.update(view_nudge.VIEW_NUDGE_SOURCE, active=False, params={"after": 4})
    loop = _loop()
    view_nudge.note_call_run(loop, ["shell_bash"] * 8)
    assert loop.recorder.notes == []
