"""The byte-equality + mutation-propagation contracts, with the FOLD as the backing.

These re-run the decisive live-plane contracts (``test_live_plane_byte_equality`` for
the classic wire, ``test_reactv2_wire_byte_equality`` for the V2 wire) against an
ARCMemory built with ``working_set_fold=True`` — so the working set is a FOLD of the
canonical ``_events`` log, not a separately-written scope. Passing here proves the
fold is a byte-exact drop-in behind the ``render_segments`` / ``render_working_set``
seam (design §2.8b), and the mutation-propagation cases are the **anti-shadow guard**:
a fold that read a stale materialization would still show a deleted/summarized segment
(sabotage d), so their propagation is the proof there is no second copy.

Both ARC backends are exercised (LocalFS + clio-core) to match the S0 sweep. Each test
gets a UNIQUE session id so the shared, process-global clio-core runtime can never leak
one test's log into another's fold.
"""

from __future__ import annotations

import uuid
from typing import Any, Iterator

import dspy
import pytest
from dspy.utils.dummies import DummyLM

from clio_agent.arc.memory import ARCMemory

from .conftest import (
    expected_trajectory_dict,
    live_plane_context,
    make_react_agent,
    stock_format_trajectory,
)

SCOPE = "agentA"


@pytest.fixture
def session() -> str:
    """A unique session id per test (isolation on the shared clio-core runtime)."""
    return "fold_" + uuid.uuid4().hex[:12]


@pytest.fixture(params=["local", "cte"])
def fold_arc(request, tmp_path) -> Iterator[ARCMemory]:
    """A fresh fold-ON ARCMemory on BOTH backends (mirrors the ``arc`` fixture)."""
    backend = request.param
    if backend == "cte":
        pytest.importorskip("clio_cte_core_ext")
        from clio_agent.arc.storage import make_arc_store

        memory = ARCMemory(store=make_arc_store(backend="cte"), working_set_fold=True)
        try:
            yield memory
        finally:
            memory.clear_all()
        return
    yield ARCMemory(data_dir=str(tmp_path / "arc"), working_set_fold=True)


@pytest.fixture(autouse=True)
def _pin_classic(monkeypatch: pytest.MonkeyPatch) -> None:
    """Pin the classic loop for the classic-wire contracts (V2 tests re-enable it)."""
    monkeypatch.setattr("clio_agent.gact.agents.runtime._reactv2_enabled", lambda: False)


def _run_scripted_loop(agent: Any, arc: ARCMemory, session: str) -> Any:
    lm = DummyLM(
        [
            {"next_thought": "search first", "next_tool_name": "search",
             "next_tool_args": '{"q": "alpha"}'},
            {"next_thought": "done", "next_tool_name": "finish", "next_tool_args": "{}"},
            {"reasoning": "because", "answer": "FINAL_ANSWER"},
        ]
    )
    with live_plane_context(arc, session=session, scope=SCOPE):
        with dspy.context(lm=lm, adapter=dspy.ChatAdapter()):
            return agent(question="find alpha")


def _populate(arc: ARCMemory, session: str, *triples: Any) -> None:
    for step, (kind, content) in enumerate(triples):
        arc.append_segment(session, SCOPE, kind, content, step=step // 3)


def _rendered_after_edit(agent: Any, arc: ARCMemory, session: str, edit: Any) -> tuple[str, str]:
    with live_plane_context(arc, session=session, scope=SCOPE):
        with dspy.context(adapter=dspy.ChatAdapter()):
            before = agent._format_trajectory({})
            edit()
            after = agent._format_trajectory({})
    return before, after


# ---- byte-equality (fold-backed) --------------------------------------------


def test_fold_render_keys_matches_stock_dspy_dict(fold_arc: ARCMemory, session: str) -> None:
    agent = make_react_agent()
    _run_scripted_loop(agent, fold_arc, session)
    keys = fold_arc.render_segments_keys(session, SCOPE)
    expected = expected_trajectory_dict(
        [
            {"thought": "search first", "tool_name": "search",
             "tool_args": {"q": "alpha"}, "observation": "SEARCH_RESULT"},
            {"thought": "done", "tool_name": "finish",
             "tool_args": {}, "observation": "Completed."},
        ]
    )
    assert list(keys.keys()) == list(expected.keys())
    assert keys["thought_0"] == "search first"
    assert keys["observation_0"] == "SEARCH_RESULT"


def test_fold_format_trajectory_byte_equal_to_stock(fold_arc: ARCMemory, session: str) -> None:
    agent = make_react_agent()
    _run_scripted_loop(agent, fold_arc, session)
    with live_plane_context(fold_arc, session=session, scope=SCOPE):
        with dspy.context(adapter=dspy.ChatAdapter()):
            keys = fold_arc.render_segments_keys(session, SCOPE)
            override = agent._format_trajectory({})
            stock = stock_format_trajectory(agent, keys)
    assert override == stock
    assert "SEARCH_RESULT" in override


# ---- mutation propagation (anti-shadow guard) -------------------------------


def test_fold_append_propagates(fold_arc: ARCMemory, session: str) -> None:
    agent = make_react_agent()
    _populate(fold_arc, session, ("thought", {"text": "T0"}), ("tool_call", {"name": "a", "args": {}}),
              ("observation", {"text": "O0"}))
    before, after = _rendered_after_edit(
        agent, fold_arc, session,
        lambda: fold_arc.append_segment(session, SCOPE, "thought", {"text": "APPENDED_X"}, step=1),
    )
    assert "APPENDED_X" not in before
    assert "APPENDED_X" in after


def test_fold_delete_propagates_absent(fold_arc: ARCMemory, session: str) -> None:
    """THE killer: a deleted segment vanishes from the next fold render (a shadow
    store would still show it)."""
    agent = make_react_agent()
    _populate(fold_arc, session, ("thought", {"text": "KEEP_T"}), ("tool_call", {"name": "a", "args": {}}),
              ("observation", {"text": "DELETE_ME"}))
    obs = [s for s in fold_arc.render_segments(session, SCOPE) if s.kind == "observation"][0]
    before, after = _rendered_after_edit(
        agent, fold_arc, session, lambda: fold_arc.delete_segments(session, SCOPE, [obs.id])
    )
    assert "DELETE_ME" in before
    assert "DELETE_ME" not in after
    assert "KEEP_T" in after


def test_fold_summarize_propagates(fold_arc: ARCMemory, session: str) -> None:
    agent = make_react_agent()
    _populate(fold_arc, session, ("thought", {"text": "ORIGINAL_THOUGHT"}),
              ("tool_call", {"name": "a", "args": {}}),
              ("observation", {"text": "ORIGINAL_OBS"}))
    ids = [s.id for s in fold_arc.render_segments(session, SCOPE)]
    before, after = _rendered_after_edit(
        agent, fold_arc, session,
        lambda: fold_arc.summarize_segments(session, SCOPE, ids, {"text": "SUMMARY_REPLACES_ALL"}),
    )
    assert "ORIGINAL_THOUGHT" in before and "ORIGINAL_OBS" in before
    assert "SUMMARY_REPLACES_ALL" in after
    assert "ORIGINAL_THOUGHT" not in after and "ORIGINAL_OBS" not in after


def test_fold_insert_propagates_at_position(fold_arc: ARCMemory, session: str) -> None:
    agent = make_react_agent()
    _populate(fold_arc, session, ("thought", {"text": "FIRST"}), ("observation", {"text": "THIRD"}))
    before, after = _rendered_after_edit(
        agent, fold_arc, session,
        lambda: fold_arc.insert_segment(session, SCOPE, 1, "thought", {"text": "INSERTED_SECOND"}),
    )
    assert "INSERTED_SECOND" not in before
    assert "INSERTED_SECOND" in after


def test_fold_append_only_is_a_prefix(fold_arc: ARCMemory, session: str) -> None:
    agent = make_react_agent()
    _populate(fold_arc, session, ("thought", {"text": "A0"}), ("tool_call", {"name": "t", "args": {}}),
              ("observation", {"text": "B0"}))
    with live_plane_context(fold_arc, session=session, scope=SCOPE):
        with dspy.context(adapter=dspy.ChatAdapter()):
            first = agent._format_trajectory({})
            fold_arc.append_segment(session, SCOPE, "thought", {"text": "A1"}, step=1)
            second = agent._format_trajectory({})
    assert second.startswith(first)


def test_fold_replace_propagates(fold_arc: ARCMemory, session: str) -> None:
    """A 1:1 replace supersedes at the same render slot (fold-backed)."""
    agent = make_react_agent()
    _populate(fold_arc, session, ("thought", {"text": "BEFORE_REPLACE"}))
    seg = fold_arc.render_segments(session, SCOPE)[0]
    before, after = _rendered_after_edit(
        agent, fold_arc, session,
        lambda: fold_arc.replace_segment(session, SCOPE, seg.id, {"text": "AFTER_REPLACE"}),
    )
    assert "BEFORE_REPLACE" in before and "AFTER_REPLACE" not in before
    assert "AFTER_REPLACE" in after and "BEFORE_REPLACE" not in after


# ---- V2 wire (fold-backed) --------------------------------------------------


_V2_STEPS = [
    {"thought": "search first", "tool_name": "search",
     "tool_args": {"q": "alpha"}, "observation": "SEARCH_RESULT"},
    {"thought": "again", "tool_name": "search",
     "tool_args": {"q": "beta"}, "observation": "SECOND_RESULT"},
]


def _populate_v2(arc: ARCMemory, session: str, steps: list[dict[str, Any]]) -> None:
    for i, s in enumerate(steps):
        arc.append_segment(session, SCOPE, "thought", {"text": s["thought"]}, step=i)
        arc.append_segment(
            session, SCOPE, "tool_call", {"name": s["tool_name"], "args": s["tool_args"]}, step=i
        )
        arc.append_segment(session, SCOPE, "observation", {"text": s["observation"]}, step=i)


def test_fold_v2_fold_matches_reference(fold_arc: ARCMemory, session: str) -> None:
    """``segments_to_messages`` over the fold reproduces the independently-built V2
    reference message list exactly (the V2 anti-shadow wire proof)."""
    from clio_agent.gact.agents.reactv2 import segments_to_messages

    from .test_reactv2_wire_byte_equality import expected_history_messages

    _populate_v2(fold_arc, session, _V2_STEPS)
    with live_plane_context(fold_arc, session=session, scope=SCOPE):
        folded = segments_to_messages(fold_arc.render_segments(session, SCOPE))
    assert folded == expected_history_messages(_V2_STEPS)


def test_fold_v2_delete_propagates_on_wire(fold_arc: ARCMemory, session: str) -> None:
    from clio_agent.gact.agents.reactv2 import segments_to_messages

    _populate_v2(fold_arc, session, _V2_STEPS)
    with live_plane_context(fold_arc, session=session, scope=SCOPE):
        obs = [s for s in fold_arc.render_segments(session, SCOPE) if s.kind == "observation"]
        fold_arc.delete_segments(session, SCOPE, [obs[-1].id])
        after = segments_to_messages(fold_arc.render_segments(session, SCOPE))
    after_text = "\n".join(str(m) for m in after)
    assert "SECOND_RESULT" not in after_text
    assert "SEARCH_RESULT" in after_text
