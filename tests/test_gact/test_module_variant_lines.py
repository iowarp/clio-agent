"""A ``dspy.BestOfN`` / ``Refine`` spawn strategy keeps one conversation line across turns.

Each try forks from the agent's base scope: it starts from the conversation as it
stands (earlier turns, and the earlier winners), never from its own line of an
earlier call. The winning try's line is recorded on the base scope, so the next turn
continues from what the user was answered with. Both are recorded plane ops (a
recorded delete of the try's previous line, appends), never a silent rewrite.
"""

from __future__ import annotations

import types
from pathlib import Path
from typing import Any, Iterator

import dspy
import pytest
from dspy.utils.dummies import DummyLM

from clio_agent.arc.memory import ARCMemory
from clio_agent.gact import context as ctx
from clio_agent.gact.agents import module_variants as mv
from clio_agent.gact.agents.clio_react_record import arc_scope, read_steps

_SESSION, _SCOPE = "sid-lines", "analyst"
_SEEN: list[tuple[int, list[str]]] = []


def test_variants_inherit_input_references_without_copying_delivery_authority(
    plane: ARCMemory,
) -> None:
    """Fork, winner selection and restart preserve content and scope-bound input proofs."""
    from clio_agent.arc.task_types import ContextBoundary, RecipientInput
    from clio_agent.gact.agents.variant_lines import fork_try, record_winner
    from clio_agent.gact.task_history import TaskHistory

    history = TaskHistory(plane)
    ctx.active_app().state.task_history = history

    def commit(scope: str, identity: str, text: str) -> None:
        content = history.write_content(_SESSION, {"text": text})
        history.record_input(
            RecipientInput(
                id=identity,
                session_id=_SESSION,
                assignment_id="assignment",
                iteration_id=identity,
                turn_id="input-turn",
                notification_ids=(),
                content=(),
                boundary=ContextBoundary(session_id=_SESSION, scope=scope, logical_time=0),
                rendered_content=(content,),
                committed_at="2026-10-17T00:00:00Z",
            )
        )

    commit(_SCOPE, "base-input", "Original human request")
    forked = fork_try(0)
    variant = _SCOPE + "#run0"
    commit(variant, "variant-input", "Accepted variant advice")
    record_winner(0, forked)
    for scope in (_SCOPE, variant):
        rows = plane.render_working_set(_SESSION, scope)
        for row in rows:
            if row.kind == "recipient_input":
                assert row.content["boundary"]["scope"] == scope
        assert any(row.kind == "user" and row.content.get("rendered_content") for row in rows)
    restored = TaskHistory(plane)
    restored.restore()
    restored.restore()
    ctx.active_app().state.task_history = restored
    assert "Original human request" in str(read_steps(plane, _SESSION, _SCOPE))
    assert "Accepted variant advice" in str(read_steps(plane, _SESSION, _SCOPE))
    assert "Original human request" in str(read_steps(plane, _SESSION, variant))
    assert "Accepted variant advice" in str(read_steps(plane, _SESSION, variant))


@pytest.fixture
def plane(tmp_path: Path) -> Iterator[ARCMemory]:
    arc = ARCMemory(data_dir=str(tmp_path / "arc"))
    app = types.SimpleNamespace(state=types.SimpleNamespace(arc=arc))
    tokens = [ctx.set_app(app), ctx.set_react_scope(_SCOPE), ctx.set_react_session(_SESSION)]
    _SEEN.clear()
    try:
        yield arc
    finally:
        for tok in reversed(tokens):
            ctx.reset(tok)


def _thoughts() -> list[str]:
    arc, session, scope = arc_scope()
    return [
        "".join(getattr(p, "text", "") for p in m.parts if type(p).__name__ == "TextPart")
        for m in read_steps(arc, session, scope)
        if m.role == "assistant"
    ]


def _write(arc: ARCMemory, text: str) -> None:
    arc.append_segment(
        _SESSION, ctx.run_keyed_scope(_SCOPE), "thought", {"text": text}, step=0, token_count=1
    )


class _Inner(dspy.Module):
    """Records what its try sees, then writes its own step on its run-keyed scope."""

    def __init__(self, label: str, *, compact_on: int = -1) -> None:
        super().__init__()
        self.predict = dspy.Predict("question -> answer")
        self.label = label
        self.compact_on = compact_on

    def forward(self, **kwargs: Any) -> Any:
        run = ctx.active_react_run()
        _SEEN.append((run, _thoughts()))
        arc = ctx.active_app().state.arc
        if run == self.compact_on:
            scope = ctx.run_keyed_scope(_SCOPE)
            live = arc.render_working_set(_SESSION, scope)
            arc.delete_segments(_SESSION, scope, [s.id for s in live])
            _write(arc, "SUMMARY")
        _write(arc, f"{self.label}-TRY{run}")
        return self.predict(**kwargs)


def _call(label: str, **inner: Any) -> None:
    strategy = mv.strategy_module(
        {"variant": "best_of_n", "n": 2, "threshold": 1.0, "rubric": "Score it."},
        agent_id=_SCOPE,
    )
    wrapped = mv.wrap_strategy(_Inner(label, **inner), _SCOPE, strategy)
    lm = DummyLM([{"answer": "a0"}, {"score": "0.1"}, {"answer": "a1"}, {"score": "0.9"}])
    with dspy.context(lm=lm):
        wrapped(question="q")


def test_every_try_starts_from_the_conversation_and_the_winner_continues_it(
    plane: ARCMemory,
) -> None:
    _write(plane, "EARLIER")

    _call("C1")
    assert _SEEN == [(0, ["EARLIER"]), (1, ["EARLIER"])]
    assert _thoughts() == ["EARLIER", "C1-TRY1"], "the winner's line is on the base scope"

    _SEEN.clear()
    _call("C2")
    # Try 0 does not continue its own C1 line: it forks from the conversation.
    assert _SEEN == [(0, ["EARLIER", "C1-TRY1"]), (1, ["EARLIER", "C1-TRY1"])]
    assert _thoughts() == ["EARLIER", "C1-TRY1", "C2-TRY1"]


def test_a_winner_that_compacted_its_fork_becomes_the_base_line(plane: ARCMemory) -> None:
    _write(plane, "EARLIER")

    _call("C1", compact_on=1)

    assert _thoughts() == ["SUMMARY", "C1-TRY1"]


def test_the_fork_and_the_winner_are_recorded_ops(plane: ARCMemory) -> None:
    _write(plane, "EARLIER")
    _call("C1")
    _call("C2")

    token = ctx.set_react_run(0)
    try:
        history = plane.list_segments(
            _SESSION, ctx.run_keyed_scope(_SCOPE), include_tombstoned=True
        )
    finally:
        ctx.reset(token)
    retired = [s.content["text"] for s in history if s.status == "tombstoned"]
    assert retired == ["EARLIER", "C1-TRY0"], "try 0's C1 line is retired, not erased"
