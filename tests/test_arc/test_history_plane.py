"""The History mode plane: an agent scope's context held in a DSPy ``History``, in memory.

It answers the four reads/writes the loop and the variant lines use, with the same
segment records clio-core holds, so the one projection (``fold_steps``) serves both modes.
"""

from __future__ import annotations

from types import SimpleNamespace

import dspy
import pytest

from clio_agent.arc import history_mode
from clio_agent.arc.history_plane import HistoryPlane, plane_for


def test_appends_render_in_order_and_live_only_after_a_delete() -> None:
    plane = HistoryPlane()
    a = plane.append_segment("s", "agent", "user", {"text": "q"}, step=0)
    b = plane.append_segment("s", "agent", "thought", {"text": "t"}, step=0)
    plane.append_segment("s", "agent", "semantic_event", {"text": "e"}, step=0)

    assert [seg.id for seg in plane.render_segments("s", "agent")][:2] == [a.id, b.id]
    assert [seg.kind for seg in plane.render_working_set("s", "agent")] == ["user", "thought"]

    assert plane.delete_segments("s", "agent", [a.id]) == 1
    assert [seg.id for seg in plane.render_working_set("s", "agent")] == [b.id]
    assert len(plane.list_segments("s", "agent", include_tombstoned=True)) == 3
    assert plane.list_segments("other", "agent") == []


def test_each_scope_is_a_dspy_history() -> None:
    plane = HistoryPlane()
    plane.append_segment("s", "agent", "user", {"text": "q"})

    history = plane.history("s", "agent")
    assert isinstance(history, dspy.History)
    assert history.messages[0]["content"] == {"text": "q"}


def test_no_plane_outside_history_mode() -> None:
    app = SimpleNamespace(state=SimpleNamespace(arc=None))

    assert plane_for(app) is None


@pytest.mark.history_mode
def test_history_mode_gives_the_app_one_plane(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(history_mode, "binding_present", lambda: False)
    history_mode.resolve()
    app = SimpleNamespace(state=SimpleNamespace(arc=None))

    plane = plane_for(app)

    assert isinstance(plane, HistoryPlane)
    assert plane_for(app) is plane


def test_a_bound_arc_is_the_plane() -> None:
    arc = object()
    app = SimpleNamespace(state=SimpleNamespace(arc=arc))

    assert plane_for(app) is arc
