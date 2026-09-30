"""An event clio-core cannot persist is a typed failure, never a logged drop.

``record_semantic_event`` persisted the event and derived the highway; a persist
failure (a clio-core RPC error, a store write refused on the event-loop thread) was
logged as ``ARC-EVENTS FAILED to persist`` and the event still went to the trace and
SSE -- the trace then held more than clio-core. clio-core is the record: a failed
persist raises, and nothing is derived from an event clio-core does not hold.
"""

from __future__ import annotations

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
