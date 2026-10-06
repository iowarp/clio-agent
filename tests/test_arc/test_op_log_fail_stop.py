"""A context op whose ``arc.op`` record cannot be written fails typed, applying nothing.

Both stores swallowed an op-logger failure ("op still applied"): the op landed in
clio-core but not on the trace that replays it (``reconstruct_arc_segments``), so the
replay contract was silently incomplete. The op now fails typed and is not applied.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from clio_agent.arc.memory import ARCMemory
from clio_agent.arc.segment_ids import ContextOpLogError
from clio_agent.arc.segments import SegmentStore
from tests.test_arc.test_segment_store import _clio_core

SID, SCOPE = "s1", "agentA"


def _refuse(*_a: Any, **_kw: Any) -> dict[str, Any]:
    raise OSError("trace volume full")


def test_the_fold_store_applies_nothing_when_the_op_log_fails(tmp_path: Path) -> None:
    arc = ARCMemory(data_dir=str(tmp_path / "arc"))
    kept = arc.append_segment(SID, SCOPE, "user", {"text": "q"}, step=0)
    arc.set_segment_op_logger(_refuse)

    with pytest.raises(ContextOpLogError) as err:
        arc.append_segment(SID, SCOPE, "user", {"text": "lost?"}, step=0)

    assert err.value.error_type == "context_op_log_failed"
    assert [s.id for s in arc.render_segments(SID, SCOPE)] == [kept.id]


def test_the_base_store_applies_nothing_when_the_op_log_fails(tmp_path: Path) -> None:
    store = SegmentStore(_clio_core(str(tmp_path)), op_logger=None)
    kept = store.append(SID, "_events", "semantic_event", {"text": "e1"})
    store._op_logger = _refuse  # the trace fails from here on

    with pytest.raises(ContextOpLogError):
        store.append(SID, "_events", "semantic_event", {"text": "e2"})

    assert [s.id for s in store.render(SID, "_events")] == [kept.id]
