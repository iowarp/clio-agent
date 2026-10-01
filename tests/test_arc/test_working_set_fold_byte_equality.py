"""The byte-equality + mutation-propagation contracts, with the FOLD as the backing.

These re-run the decisive live-plane contracts (``test_clio_react_wire_byte_equality``)
against an ARCMemory built with ``working_set_fold=True`` — so the working set is a
FOLD of the canonical ``_events`` log, not a separately-written scope. Passing here
proves the fold is a byte-exact drop-in behind the ``render_segments`` /
``render_working_set`` seam (design §2.8b), and the mutation-propagation cases are
the **anti-shadow guard**: a fold that read a stale materialization would still show
a deleted/summarized segment (sabotage d), so their propagation is the proof there
is no second copy. Mutations are observed through the loop's context read
(``fold_steps`` over the fold render).

Both ARC backends are exercised (LocalFS + clio-core) to match the S0 sweep. Each test
gets a UNIQUE session id so the shared, process-global clio-core runtime can never leak
one test's log into another's fold.
"""

from __future__ import annotations

import uuid
from typing import Any, Iterator

import pytest

from clio_agent.arc.memory import ARCMemory
from clio_agent.gact.agents.clio_react_record import fold_steps

from .conftest import live_plane_context

SCOPE = "agentA"


@pytest.fixture
def session() -> str:
    """A unique session id per test (isolation on the shared clio-core runtime)."""
    return "fold_" + uuid.uuid4().hex[:12]


@pytest.fixture
def fold_arc() -> Iterator[ARCMemory]:
    """A fresh ARCMemory on clio-core (the only store), in this test's namespace."""
    from clio_agent.arc.storage import make_arc_store

    memory = ARCMemory(store=make_arc_store(backend="cte"), working_set_fold=True)
    try:
        yield memory
    finally:
        memory.clear_all()


def _populate(arc: ARCMemory, session: str, *triples: Any) -> None:
    for step, (kind, content) in enumerate(triples):
        arc.append_segment(session, SCOPE, kind, content, step=step // 3)


def _wire_text(arc: ARCMemory, session: str) -> str:
    """The loop's context messages over the fold render, flattened to text."""
    msgs = fold_steps(arc.render_segments(session, SCOPE))
    return "\n".join(str(m) for m in msgs)


def _rendered_after_edit(arc: ARCMemory, session: str, edit: Any) -> tuple[str, str]:
    with live_plane_context(arc, session=session, scope=SCOPE):
        before = _wire_text(arc, session)
        edit()
        after = _wire_text(arc, session)
    return before, after


# ---- byte-equality (fold-backed) --------------------------------------------


# ---- mutation propagation (anti-shadow guard) -------------------------------


def test_fold_append_propagates(fold_arc: ARCMemory, session: str) -> None:
    _populate(
        fold_arc,
        session,
        ("thought", {"text": "T0"}),
        ("tool_call", {"id": "c0", "name": "a", "args": {}}),
        ("observation", {"call_id": "c0", "text": "O0", "is_error": False}),
    )
    before, after = _rendered_after_edit(
        fold_arc,
        session,
        lambda: fold_arc.append_segment(session, SCOPE, "thought", {"text": "APPENDED_X"}, step=1),
    )
    assert "APPENDED_X" not in before
    assert "APPENDED_X" in after


def test_fold_delete_propagates_absent(fold_arc: ARCMemory, session: str) -> None:
    """THE killer: a deleted call + its observation vanish from the next fold render (a
    shadow store would still show them). The pair is deleted together: a call left
    without its result is an incoherent plane the fold refuses."""
    _populate(
        fold_arc,
        session,
        ("thought", {"text": "KEEP_T"}),
        ("tool_call", {"id": "c0", "name": "a", "args": {}}),
        ("observation", {"call_id": "c0", "text": "DELETE_ME", "is_error": False}),
    )
    pair = [
        s.id
        for s in fold_arc.render_segments(session, SCOPE)
        if s.kind in ("tool_call", "observation")
    ]
    before, after = _rendered_after_edit(
        fold_arc, session, lambda: fold_arc.delete_segments(session, SCOPE, pair)
    )
    assert "DELETE_ME" in before
    assert "DELETE_ME" not in after
    assert "KEEP_T" in after


def test_fold_summarize_propagates(fold_arc: ARCMemory, session: str) -> None:
    _populate(
        fold_arc,
        session,
        ("thought", {"text": "ORIGINAL_THOUGHT"}),
        ("tool_call", {"id": "c0", "name": "a", "args": {}}),
        ("observation", {"call_id": "c0", "text": "ORIGINAL_OBS", "is_error": False}),
    )
    ids = [s.id for s in fold_arc.render_segments(session, SCOPE)]
    before, after = _rendered_after_edit(
        fold_arc,
        session,
        lambda: fold_arc.summarize_segments(session, SCOPE, ids, {"text": "SUMMARY_REPLACES_ALL"}),
    )
    assert "ORIGINAL_THOUGHT" in before and "ORIGINAL_OBS" in before
    assert "SUMMARY_REPLACES_ALL" in after
    assert "ORIGINAL_THOUGHT" not in after and "ORIGINAL_OBS" not in after


def test_fold_insert_propagates_at_position(fold_arc: ARCMemory, session: str) -> None:
    """Insert is observed at the fold-render layer: ``fold_steps`` groups by step, so the anti-shadow proof for
    positional insert is the ordered ``render_segments`` view itself."""
    _populate(fold_arc, session, ("thought", {"text": "FIRST"}), ("observation", {"text": "THIRD"}))
    with live_plane_context(fold_arc, session=session, scope=SCOPE):
        before = [s.content.get("text") for s in fold_arc.render_segments(session, SCOPE)]
        fold_arc.insert_segment(session, SCOPE, 1, "thought", {"text": "INSERTED_SECOND"})
        after = [s.content.get("text") for s in fold_arc.render_segments(session, SCOPE)]
    assert "INSERTED_SECOND" not in before
    assert after == ["FIRST", "INSERTED_SECOND", "THIRD"]


def test_fold_append_only_is_a_prefix(fold_arc: ARCMemory, session: str) -> None:
    """Appends extend the folded message list; the prior messages are a stable prefix."""
    _populate(
        fold_arc,
        session,
        ("thought", {"text": "A0"}),
        ("tool_call", {"id": "c0", "name": "t", "args": {}}),
        ("observation", {"call_id": "c0", "text": "B0", "is_error": False}),
    )
    with live_plane_context(fold_arc, session=session, scope=SCOPE):
        first = fold_steps(fold_arc.render_segments(session, SCOPE))
        fold_arc.append_segment(session, SCOPE, "thought", {"text": "A1"}, step=1)
        second = fold_steps(fold_arc.render_segments(session, SCOPE))
    assert len(second) >= len(first)
    assert second[: len(first)] == first


def test_fold_replace_propagates(fold_arc: ARCMemory, session: str) -> None:
    """A 1:1 replace supersedes at the same render slot (fold-backed)."""
    _populate(fold_arc, session, ("thought", {"text": "BEFORE_REPLACE"}))
    seg = fold_arc.render_segments(session, SCOPE)[0]
    before, after = _rendered_after_edit(
        fold_arc,
        session,
        lambda: fold_arc.replace_segment(session, SCOPE, seg.id, {"text": "AFTER_REPLACE"}),
    )
    assert "BEFORE_REPLACE" in before and "AFTER_REPLACE" not in before
    assert "AFTER_REPLACE" in after and "BEFORE_REPLACE" not in after


# ---- loop context (fold-backed) --------------------------------------------------


_STEPS = [
    {
        "thought": "search first",
        "tool_name": "search",
        "tool_args": {"q": "alpha"},
        "observation": "SEARCH_RESULT",
    },
    {
        "thought": "again",
        "tool_name": "search",
        "tool_args": {"q": "beta"},
        "observation": "SECOND_RESULT",
    },
]


def _populate_steps(arc: ARCMemory, session: str, steps: list[dict[str, Any]]) -> None:
    for i, s in enumerate(steps):
        call_id = f"call_{i}_0"
        arc.append_segment(session, SCOPE, "thought", {"text": s["thought"]}, step=i)
        arc.append_segment(
            session,
            SCOPE,
            "tool_call",
            {"id": call_id, "name": s["tool_name"], "args": s["tool_args"]},
            step=i,
        )
        arc.append_segment(
            session,
            SCOPE,
            "observation",
            {"call_id": call_id, "text": s["observation"], "is_error": False},
            step=i,
        )


def test_fold_steps_match_reference(fold_arc: ARCMemory, session: str) -> None:
    """``fold_steps`` over the fold reproduces the independently-built reference
    messages exactly (the anti-shadow wire proof)."""
    from .test_clio_react_wire_byte_equality import expected_messages

    _populate_steps(fold_arc, session, _STEPS)
    with live_plane_context(fold_arc, session=session, scope=SCOPE):
        folded = fold_steps(fold_arc.render_segments(session, SCOPE))
    assert folded == expected_messages(_STEPS)


def test_fold_delete_propagates_on_the_loop_context(fold_arc: ARCMemory, session: str) -> None:
    """Deleting the last step's call + observation (a coherent unit) removes its result
    from the loop context; the earlier step is untouched."""
    _populate_steps(fold_arc, session, _STEPS)
    with live_plane_context(fold_arc, session=session, scope=SCOPE):
        last = [
            s.id
            for s in fold_arc.render_segments(session, SCOPE)
            if s.kind in ("tool_call", "observation")
        ][-2:]
        fold_arc.delete_segments(session, SCOPE, last)
        after = fold_steps(fold_arc.render_segments(session, SCOPE))
    after_text = "\n".join(str(m) for m in after)
    assert "SECOND_RESULT" not in after_text
    assert "SEARCH_RESULT" in after_text
