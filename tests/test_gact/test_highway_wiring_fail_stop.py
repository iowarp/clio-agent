"""The highway's wiring and the durable trace never drop silently.

``_set_app_arc`` / ``_wire_arc_op_logger`` swallowed a wiring failure (the trace, SSE and
hooks then silently got nothing), and the trace writer thread logged and dropped an
event it could not write. A wiring failure raises; a trace write failure is kept and
raised, typed, by the next ``emit`` or ``flush`` of that trace.
"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from clio_agent.gact.runtime.globals import _set_app_arc
from clio_agent.gact.semantic_events import SemanticEvent
from clio_agent.gact.semantic_trace_file import FileSemanticTraceBackend, TraceWriteError


class _ArcRefusingWiring:
    def set_segment_op_logger(self, _fn: Any) -> None:
        return None

    def set_highway_sink(self, _fn: Any) -> None:
        raise RuntimeError("wiring refused")


def test_a_highway_wiring_failure_raises() -> None:
    app = SimpleNamespace(state=SimpleNamespace(arc=None, semantic_event_sink=None))

    with pytest.raises(RuntimeError, match="wiring refused"):
        _set_app_arc(app, _ArcRefusingWiring())


def _event(sid: str = "s1") -> SemanticEvent:
    return SemanticEvent(
        event_type="turn.started",
        session_id=sid,
        trace_id="t",
        occurred_at="2026-10-01T00:00:00+00:00",
    )


def test_a_trace_write_failure_is_raised_by_the_next_flush(tmp_path: Path) -> None:
    blocker = tmp_path / "trace.jsonl"
    blocker.mkdir()  # a directory where the trace file should be: every write fails
    backend = FileSemanticTraceBackend(blocker)

    backend.emit(_event())
    with pytest.raises(TraceWriteError) as err:
        backend.flush()
    assert err.value.error_type == "trace_write_failed"

    with pytest.raises(TraceWriteError):  # the failure is not forgotten after one raise
        backend.emit(_event())
