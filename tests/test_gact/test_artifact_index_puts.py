"""A writer turn's artifact-index puts leave together (``gact.artifacts.index_puts``).

A tool call's transform cost three artifact-index round trips, one after another on the
session's history writer: its session page and the transform marked pending before the
call's event, and the confirmed transform after it (ledger CT5). A turn's puts now go as
one multi-put before its events and one after, with the order the pending mark relies on.
"""

from __future__ import annotations

import json
import threading
import uuid
from functools import partial
from typing import Any

from clio_agent.arc.memory import ARCMemory
from clio_agent.gact.artifacts.registry import ArtifactRegistry
from clio_agent.gact.artifacts.registry_store import StoreBackend, index_key
from clio_agent.gact.artifacts.transform_types import Instrument
from clio_agent.gact.artifacts.transforms import TransformRecord, TransformStatus
from clio_agent.gact.highway_writer import HighwayWriter
from clio_agent.gact.semantic_events import SemanticEvent


class _Store:
    def __init__(self) -> None:
        self.records: dict[str, bytes] = {}
        self.calls: list[tuple[str, int]] = []

    def get(self, _kind: str, name: str) -> bytes | None:
        return self.records.get(name)

    def put(self, _kind: str, name: str, data: bytes) -> None:
        self.calls.append(("put", 1))
        self.records[name] = data

    def put_many(self, _kind: str, records: list[Any]) -> None:
        self.calls.append(("put_many", len(records)))
        for record in records:
            self.records[record.name] = record.data


def _transform(sid: str, n: int) -> TransformRecord:
    return TransformRecord(
        call_id=f"call_{n:03d}",
        event_id=f"evt_{n:03d}",
        session_id=sid,
        workspace_id="ws",
        status=TransformStatus.SUCCESS,
        instrument=Instrument(tool="fs_list_directory", args={}),
        started_at="2026-11-05T00:00:00Z",
        ended_at="2026-11-05T00:00:01Z",
    )


def test_ten_transforms_cost_two_multi_puts_and_end_confirmed(tmp_path) -> None:
    """SABOTAGE: run each queued ``IndexPut`` as its own operation in
    ``HighwayWriter._record`` (drop the ``isinstance`` branch) and the store sees twenty
    single puts ahead of the events; drop ``collecting`` around the after-hooks and the
    ten confirms are ten more."""
    arc = ARCMemory(data_dir=str(tmp_path / "arc"))
    sid = f"sess_idx_{uuid.uuid4().hex[:8]}"
    store = _Store()
    registry = ArtifactRegistry(
        backend=StoreBackend(store, event_check=lambda _s, _e: True)  # type: ignore[arg-type]
    )
    writer = HighwayWriter(arc)
    gate = threading.Event()
    run = writer._run
    writer._run = lambda: (gate.wait(10), run())  # type: ignore[method-assign]
    for n in range(10):
        record = _transform(sid, n)
        queue = partial(writer.submit_operation, sid)
        assert registry.record_transform(record, fresh=True, write_behind=queue) is True
        event = SemanticEvent(
            event_type="artifact.transform.recorded",
            session_id=sid,
            trace_id="trace_t1",
            turn_id="t1",
            payload={"event_id": record.event_id},
            occurred_at="2026-11-05T00:00:00+00:00",
        )
        writer.submit(sid, event, partial(registry.confirm_event, record.event_id))
    gate.set()
    writer.flush(sid, timeout=30)
    writer.close()

    assert [kind for kind, _n in store.calls] == ["put_many", "put_many"], store.calls
    assert store.calls[0][1] == 11  # ten transforms and one session page (last write wins)
    page = json.loads(store.records[index_key("s", sid)])
    assert page == [f"call_{n:03d}" for n in range(10)]
    for n in range(10):
        body = json.loads(store.records[index_key("t", f"call_{n:03d}")])
        assert body["pending"] == {}  # confirmed once its event was recorded
