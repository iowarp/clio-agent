"""An event clio-core cannot persist is a typed failure, never a logged drop.

``record_semantic_event`` persisted the event and derived the highway; a persist
failure (a clio-core RPC error, a store write refused on the event-loop thread) was
logged as ``ARC-EVENTS FAILED to persist`` and the event still went to the trace and
SSE -- the trace then held more than clio-core. clio-core is the record: a failed
persist raises, and nothing is derived from an event clio-core does not hold.
"""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import pytest

from clio_agent.arc.memory import ARCMemory
from clio_agent.gact.semantic_events import SemanticEvent


class _StoreDown(RuntimeError):
    pass


def test_a_failed_persist_raises_and_derives_nothing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    arc = ARCMemory(data_dir=str(tmp_path / "arc"))
    derived: list[object] = []
    arc.set_highway_sink(derived.append)

    def refuse(_event: object) -> None:
        raise _StoreDown("clio-core refused the write")

    monkeypatch.setattr(arc, "on_semantic_event", refuse)
    event = SemanticEvent(
        event_type="turn.started",
        session_id="s1",
        trace_id="trace_t1",
        turn_id="t1",
        occurred_at="2026-09-30T00:00:00+00:00",
    )

    with pytest.raises(_StoreDown):
        arc.record_semantic_event(event)
    assert derived == [], "nothing is derived from an event clio-core does not hold"


@pytest.fixture
def refusing_app(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[object]:
    """A real app on clio-core whose event writes are refused, bound as the active app."""
    from clio_agent.gact import context as ctx
    from clio_agent.gact.app import build_app

    app = build_app(
        sessions_path=tmp_path / "s.json", arc=ARCMemory(data_dir=str(tmp_path / "arc"))
    )

    def refuse(_event: object) -> None:
        raise _StoreDown("clio-core refused the write")

    monkeypatch.setattr(app.state.arc, "on_semantic_event", refuse)
    tokens = [ctx.set_app(app), ctx.set_session_id("s1")]
    try:
        yield app
    finally:
        for token in reversed(tokens):
            ctx.reset(token)


@pytest.mark.usefixtures("refusing_app")
def test_a_react_step_clio_core_cannot_record_fails_the_turn() -> None:
    """Found in the History mode check: the step emitter swallowed every failure."""
    from clio_agent.gact.runtime.globals import _emit_react_step_event

    with pytest.raises(_StoreDown):
        _emit_react_step_event(
            expert_id="main",
            expert_span_id="e1",
            step_span_id="s1",
            step_index=0,
            thought="t",
            reasoning="",
            tool_calls=[],
            is_finish=True,
        )


@pytest.mark.usefixtures("refusing_app")
def test_an_expert_lifecycle_clio_core_cannot_record_fails_the_turn() -> None:
    from clio_agent.gact.runtime.globals import _emit_expert_lifecycle_event

    with pytest.raises(_StoreDown):
        _emit_expert_lifecycle_event(
            "expert.lifecycle.started",
            expert_id="main",
            expert_span_id="e1",
            status="running",
            payload={},
        )
