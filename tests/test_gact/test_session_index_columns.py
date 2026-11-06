"""The session index answers the metrics, the controls and the boot reconciliation.

``/v1/metrics`` sums counters kept as index columns, the autonomy controls and the
restart reconciliation select their sessions by index column, and the
``session_record_columns`` store migration fills the columns of rows indexed before
they existed -- none of them reads every session record.
"""

from __future__ import annotations

import json
from dataclasses import astuple
from types import SimpleNamespace
from typing import Any, cast

import pytest

from clio_agent.arc.live import _MemoryStore
from clio_agent.gact import store_migrations
from clio_agent.gact.session_records import KIND, PageQuery, SessionRecords, bucket_of
from clio_agent.gact.sessions import SessionStore


class _CountingStore(_MemoryStore):
    """Counts the session records read (``row/<id>``)."""

    def __init__(self) -> None:
        super().__init__()
        self.row_reads: list[str] = []

    def get(self, kind: str, name: str) -> bytes | None:
        if name.startswith("row/"):
            self.row_reads.append(name)
        return super().get(kind, name)


def _cold(backing: Any) -> SessionStore:
    """A restarted store over ``backing``: the index loaded, no row resident."""
    store = SessionStore(store=backing)
    backing.row_reads.clear()
    return store


def test_totals_come_from_the_index_without_reading_a_record() -> None:
    backing = _CountingStore()
    store = SessionStore(store=backing)
    a = store.create(workspace_id="ws", title="a").id
    b = store.create(workspace_id="ws", title="b").id
    store.update(a, status="running", message_count=4, add_tokens_input=10, add_cost_usd=0.5)
    store.update(b, message_count=2, add_tokens_output=7, add_cost_usd=0.25)
    store.flush()
    totals = _cold(backing).totals()
    assert backing.row_reads == []
    assert totals["total"] == 2
    assert totals["by_status"] == {"running": 1, "idle": 1}
    assert (totals["messages"], totals["tokens_input"], totals["tokens_output"]) == (6, 10, 7)
    assert totals["cost_usd"] == pytest.approx(0.75)


def test_metrics_route_reads_no_session_record() -> None:
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    from clio_agent.gact.metrics_counters import MetricsCounters
    from clio_agent.gact.routes.system import register_system_routes

    backing = _CountingStore()
    store = SessionStore(store=backing)
    sid = store.create(workspace_id="ws", title="a").id
    store.update(sid, message_count=3, add_tokens_input=5, add_tokens_output=6)
    store.flush()
    app = FastAPI()
    app.state.sessions = _cold(backing)
    app.state.started_at = 0
    app.state.metrics_counters = MetricsCounters()
    register_system_routes(app, cast(Any, None))
    body = TestClient(app).get("/v1/metrics").json()
    assert backing.row_reads == []
    assert body["sessions"]["total"] == 1 and body["messages"]["total"] == 3
    assert (body["tokens"]["input_total"], body["tokens"]["output_total"]) == (5, 6)


def test_control_columns_select_only_the_sessions_that_need_them() -> None:
    backing = _CountingStore()
    store = SessionStore(store=backing)
    ids = [store.create(workspace_id="ws", title=f"t{i}").id for i in range(6)]
    store.update(ids[1], metadata_patch={"goal": {"active": True}})
    store.update(ids[2], metadata_patch={"loop": {"active": True}})
    store.update(ids[3], metadata_patch={"execution_gate": "task_1"})
    store.update(ids[4], model={"provider_id": "p", "model_id": "m"})
    store.update(ids[5], metadata_patch={"memory_last_pass": {"status": "completed"}})
    store.flush()
    cold = _cold(backing)
    assert set(cold.ids(PageQuery(archived=None, autonomous=True))) == {ids[1], ids[2]}
    assert cold.ids(PageQuery(archived=None, gated=True)) == [ids[3]]
    assert cold.ids(PageQuery(archived=None, model_selected=True)) == [ids[4]]
    assert cold.ids(PageQuery(archived=None, memory_runs=True)) == [ids[5]]
    assert [s.id for s in cold.select(PageQuery(archived=None, gated=True))] == [ids[3]]
    assert backing.row_reads == [f"row/{ids[3]}"]
    store.update(ids[1], metadata_patch={"goal": {"active": False}})
    assert store.ids(PageQuery(archived=None, autonomous=True)) == [ids[2]]


def test_the_store_migration_fills_columns_of_rows_indexed_before_them() -> None:
    backing = _CountingStore()
    store = SessionStore(store=backing)
    sid = store.create(workspace_id="ws", title="old").id
    store.update(sid, message_count=9, metadata_patch={"goal": {"active": True}})
    store.flush()
    # Rewrite the bucket as an older CLIO wrote it: only the first ten columns.
    bucket = bucket_of(sid)
    rows = json.loads(backing.get(KIND, bucket) or b"{}")
    backing.put(KIND, bucket, json.dumps({k: v[:10] for k, v in rows.items()}).encode())
    old = SessionStore(store=backing)
    assert old.totals()["messages"] == 0
    assert old.ids(PageQuery(archived=None, autonomous=True)) == []
    app = SimpleNamespace(state=SimpleNamespace(sessions=old))
    details = store_migrations.migration("session_record_columns").run(app)
    assert details == {"sessions": 1, "reindexed": 1}
    old.flush()
    restarted = SessionStore(store=backing)
    assert restarted.totals()["messages"] == 9
    assert restarted.ids(PageQuery(archived=None, autonomous=True)) == [sid]
    # A second run changes nothing.
    assert store_migrations.migration("session_record_columns").run(app)["reindexed"] == 0


def test_reindex_ignores_unknown_sessions() -> None:
    records = SessionRecords()
    store = SessionStore()
    sid = store.create(workspace_id="ws", title="x").id
    columns = store._records._columns[sid]  # noqa: SLF001 - the row's own columns
    assert records.reindex([columns]) == 0
    assert len(astuple(columns)) > 10


def test_restart_reconciliation_reads_only_running_rows() -> None:
    from clio_agent.gact import session_store

    backing = _CountingStore()
    store = SessionStore(store=backing)
    ids = [store.create(workspace_id="ws", title=f"t{i}").id for i in range(5)]
    store.update(ids[2], status="running")
    store.flush()
    cold = _cold(backing)
    selected: list[str] = []
    real = cold.select

    def spy(query: PageQuery) -> list[Any]:
        rows = real(query)
        selected.extend(row.id for row in rows)
        return rows

    cold.select = spy  # type: ignore[method-assign]
    app = SimpleNamespace(state=SimpleNamespace(sessions=cold))
    import clio_agent.gact.transcript_file as transcript_file

    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(transcript_file, "has_durable_transcript_store", lambda _app: True)
        patch.setattr(transcript_file, "load_durable_transcript", lambda _app, _sid: [])
        session_store._reconcile_restart_interrupted_sessions(app)
    assert selected == [ids[2]]
    assert cold.get(ids[2]).status == "error"
