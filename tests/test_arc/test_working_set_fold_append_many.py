"""A step's segments appended as one write (``append_many``): one op, one lane put.

X2 (2026-11-04) measured an agent step's writes at 23-173 ms: each thought, call and
observation was its own ``arc.op`` and its own lane put.
"""

from __future__ import annotations

import uuid
from typing import Any

import pytest

from clio_agent.arc import lane_writer
from clio_agent.arc.live import _MemoryStore
from clio_agent.arc.memory import ARCMemory

SCOPE = "agentA"


def _rows(count: int) -> list[tuple[str, dict[str, Any], int]]:
    return [("observation", {"text": f"o{n}"}, 1) for n in range(count)]


def _texts(arc: ARCMemory, session: str) -> list[str]:
    return [s.content["text"] for s in arc.render_segments(session, SCOPE)]


def test_a_batch_is_one_lane_put_and_renders_in_order(monkeypatch: pytest.MonkeyPatch) -> None:
    """SABOTAGE: write each atom with ``_write`` in ``append_many`` and three lane puts
    are made instead of one."""
    session = "am_" + uuid.uuid4().hex[:12]
    store = _MemoryStore()
    arc = ARCMemory(store=store, working_set_fold=True)
    arc.append_segment(session, SCOPE, "thought", {"text": "before"}, step=0)
    puts: list[list[str]] = []
    put_many = store.put_many

    def counting(kind: str, records: Any) -> Any:
        puts.append([r.name for r in records])
        return put_many(kind, records)

    monkeypatch.setattr(store, "put_many", counting)
    written = arc.append_segments(session, SCOPE, _rows(3), step=1)
    assert [s.content["text"] for s in written] == ["o0", "o1", "o2"]
    lane_puts = [names for names in puts if any("~w~" in n or "/w/" in n for n in names)]
    assert len(lane_puts) == 1
    assert _texts(arc, session) == ["before", "o0", "o1", "o2"]
    # A fresh store over the same records reads them back in the same order.
    reloaded = ARCMemory(store=store, working_set_fold=True)
    assert _texts(reloaded, session) == ["before", "o0", "o1", "o2"]


def test_a_batch_across_a_full_chunk_rolls_and_counts_every_slot(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Capacity 2: a batch of 3 after one atom fills chunk 1 and puts 2 in chunk 2; the
    next append then rolls to chunk 3. SABOTAGE: reserve each slot with
    ``chunk_for_append`` and chunk 2 is counted short (the next append joins it)."""
    monkeypatch.setattr(lane_writer, "_chunk_capacity", lambda: 2)
    session = "am_" + uuid.uuid4().hex[:12]
    store = _MemoryStore()
    arc = ARCMemory(store=store, working_set_fold=True)
    arc.append_segment(session, SCOPE, "thought", {"text": "t"}, step=0)
    arc.append_segments(session, SCOPE, _rows(3), step=1)
    arc.append_segment(session, SCOPE, "thought", {"text": "after"}, step=2)
    fold = arc._segments
    index = fold._session(session).index
    held = [len(fold._segs(session, chunk)) for chunk in sorted(index.chunks)]
    assert held == [2, 2, 1]
    assert _texts(arc, session) == ["t", "o0", "o1", "o2", "after"]
    assert _texts(ARCMemory(store=store, working_set_fold=True), session) == [
        "t", "o0", "o1", "o2", "after",
    ]  # fmt: skip


def test_a_batch_is_one_context_op(monkeypatch: pytest.MonkeyPatch) -> None:
    session = "am_" + uuid.uuid4().hex[:12]
    arc = ARCMemory(store=_MemoryStore(), working_set_fold=True)
    ops: list[tuple[str, int]] = []
    emit = arc._segments._emit_op

    def counting(op: str, *args: Any, **kwargs: Any) -> Any:
        ops.append((op, len(kwargs.get("written") or [])))
        return emit(op, *args, **kwargs)

    monkeypatch.setattr(arc._segments, "_emit_op", counting)
    arc.append_segments(session, SCOPE, _rows(4), step=0)
    assert ops == [("append", 4)]
