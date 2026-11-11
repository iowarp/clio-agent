"""The session index answers the metrics, the controls and the boot reconciliation.

``/v1/metrics`` sums counters kept as index columns, the autonomy controls and the
restart marking select their sessions by index column, and the
``session_record_columns`` store migration fills the columns of rows indexed before
they existed -- none of them reads every session record.
"""

from __future__ import annotations

import dataclasses
import json
from dataclasses import astuple, dataclass
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from typing import Any, cast

import pytest

from clio_agent.arc.live import _MemoryStore
from clio_agent.gact import store_migrations
from clio_agent.gact.session_index_columns import (
    BASE_FIELDS,
    EXTENSION_PREFIX,
    base_values,
    extension_name,
)
from clio_agent.gact.session_records import (
    INDEX_BUCKETS,
    KIND,
    PageQuery,
    SessionRecords,
    bucket_of,
)
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
    totals = _cold(backing).index.totals()
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


class _Index:
    def __init__(self) -> None:
        self.markers: dict[str, dict[str, Any]] = {}

    def done_marker(self, name: str) -> dict[str, Any] | None:
        return self.markers.get(name)

    def put_done_marker(self, name: str, value: dict[str, Any]) -> None:
        self.markers[name] = value


class _History:
    """The task history the migration runner keeps its markers in."""

    def __init__(self, store: Any) -> None:
        self.index = _Index()
        no_events = SimpleNamespace(sessions_with_scope=lambda _scope: [])
        self.arc = SimpleNamespace(_store=store, _segments=no_events)

    def confirm_write(self, write: Any) -> None:
        write()


_COLUMNS = store_migrations.migration("session_record_columns")


def _migration_app(sessions: Any, history: _History) -> Any:
    return SimpleNamespace(state=SimpleNamespace(sessions=sessions, task_history=history))


def _as_older_clio_wrote(backing: _CountingStore) -> None:
    """Drop every ``columns/<nn>`` record: the store an older CLIO left behind."""
    for kind, name in list(backing._data):  # noqa: SLF001 - the test's own store
        if kind == KIND and name.startswith(EXTENSION_PREFIX):
            del backing._data[(kind, name)]  # noqa: SLF001


def test_a_write_stores_the_new_columns_beside_the_bucket_an_older_clio_reads() -> None:
    backing = _CountingStore()
    store = SessionStore(store=backing)
    sid = store.create(workspace_id="ws", title="t").id
    store.update(sid, message_count=4, metadata_patch={"goal": {"active": True}})
    store.flush()
    base = json.loads(backing.get(KIND, bucket_of(sid)) or b"{}")
    assert base[sid] == list(base_values(store.index.columns(PageQuery(archived=None))[0]))
    extension = json.loads(backing.get(KIND, extension_name(bucket_of(sid))) or b"{}")
    named = dict(zip(extension["fields"], extension["rows"][sid], strict=True))
    assert named["message_count"] == 4 and named["autonomous"] is True


def test_an_older_clio_reads_the_index_this_one_wrote() -> None:
    """The downgrade: the 0.9.5 loader builds its 10-column row from ``index/<nn>``."""

    @dataclass(frozen=True)
    class OlderRecordColumns:  # the loader's row before the extension columns
        id: str
        workspace_id: str
        parent_session_id: str
        kind: str
        archived: bool
        created_at: str
        recent_at: str
        interaction_known: bool
        status: str = ""
        title: str = ""

    backing = _CountingStore()
    store = SessionStore(store=backing)
    sid = store.create(workspace_id="ws", title="t").id
    store.update(sid, message_count=4, model={"provider_id": "p", "model_id": "m"})
    store.flush()
    loaded = [
        OlderRecordColumns(*values)  # exactly what the older bind does per bucket
        for number in range(INDEX_BUCKETS)
        for values in json.loads(backing.get(KIND, f"index/{number:02d}") or b"{}").values()
    ]
    assert [(row.id, row.title) for row in loaded] == [(sid, "t")]


def test_the_migration_fills_the_columns_of_rows_an_older_clio_indexed() -> None:
    backing = _CountingStore()
    store = SessionStore(store=backing)
    sid = store.create(workspace_id="ws", title="old").id
    store.update(sid, message_count=9, metadata_patch={"goal": {"active": True}})
    store.flush()
    _as_older_clio_wrote(backing)
    old = SessionStore(store=backing)
    assert old.index.unfilled_ids() == [sid]
    assert old.index.totals()["messages"] == 0
    history = _History(backing)
    app = _migration_app(old, history)
    core_only = SimpleNamespace(state=SimpleNamespace(task_history=history))
    assert store_migrations.is_owed(app, _COLUMNS) and store_migrations.is_owed(core_only, _COLUMNS)

    result = store_migrations.run_migration(app, _COLUMNS)

    assert (result.status, result.details) == ("done", {"filled": 1, "reindexed": 1})
    old.flush()
    restarted = SessionStore(store=backing)
    assert restarted.index.unfilled_ids() == []
    assert restarted.index.totals()["messages"] == 9
    assert restarted.ids(PageQuery(archived=None, autonomous=True)) == [sid]
    # Migrated: the marker holds, nothing is owed (bound index or store alone).
    again = _migration_app(restarted, history)
    assert not store_migrations.is_owed(again, _COLUMNS)
    assert not store_migrations.is_owed(core_only, _COLUMNS)
    assert store_migrations.run_migration(again, _COLUMNS).status == "already_done"


def test_the_migration_is_recorded_done_on_a_new_store_without_running() -> None:
    backing = _CountingStore()
    history = _History(backing)
    app = _migration_app(SessionStore(store=backing), history)
    item = dataclasses.replace(_COLUMNS, has_input=lambda _app: False)  # a store with nothing

    result = store_migrations.run_migration(app, item)

    assert result.status == "done" and result.details["input"] == "none"
    assert history.index.done_marker("session_record_columns") is not None
    assert not store_migrations.is_owed(app, _COLUMNS)


def test_rows_an_older_clio_rewrote_after_the_migration_are_owed_again() -> None:
    """Downgrade, then upgrade: the extension checksum no longer matches the bucket."""
    backing = _CountingStore()
    store = SessionStore(store=backing)
    ids = [store.create(workspace_id="ws", title=f"t{i}").id for i in range(3)]
    store.update(ids[0], metadata_patch={"execution_gate": "task_1"})
    store.flush()
    history = _History(backing)
    history.index.put_done_marker("session_record_columns", {"filled": 0})
    # The older CLIO retitles ids[0]: it rewrites index/<nn> and never columns/<nn>.
    bucket = bucket_of(ids[0])
    rows = json.loads(backing.get(KIND, bucket) or b"{}")
    rows[ids[0]][BASE_FIELDS.index("title")] = "renamed by the older CLIO"
    backing.put(KIND, bucket, json.dumps(rows, separators=(",", ":")).encode())

    upgraded = SessionStore(store=backing)
    assert ids[0] in upgraded.index.unfilled_ids()
    core_only = SimpleNamespace(state=SimpleNamespace(task_history=history))
    assert store_migrations.is_owed(core_only, _COLUMNS)  # `clio-agent migrate` sees it
    app = _migration_app(upgraded, history)
    assert store_migrations.run_migration(app, _COLUMNS).status == "done"
    assert upgraded.index.unfilled_ids() == []
    upgraded.flush()
    restarted = SessionStore(store=backing)
    assert restarted.ids(PageQuery(archived=None, gated=True)) == [ids[0]]
    assert restarted.get(ids[0]).title == "t0"  # the record is the truth the fill reads
    assert not store_migrations.is_owed(_migration_app(restarted, history), _COLUMNS)


def test_reindex_ignores_unknown_sessions() -> None:
    records = SessionRecords()
    store = SessionStore()
    sid = store.create(workspace_id="ws", title="x").id
    columns = store.index.columns(PageQuery(archived=None, ids=frozenset({sid})))[0]
    assert records.reindex([columns]) == 0
    assert len(astuple(columns)) > len(BASE_FIELDS)


def test_restart_marking_reads_only_running_rows() -> None:
    from clio_agent.gact import restart_interruptions

    backing = _CountingStore()
    store = SessionStore(store=backing)
    ids = [store.create(workspace_id="ws", title=f"t{i}").id for i in range(5)]
    store.update(ids[2], status="running")
    store.flush()
    cold = _cold(backing)
    app = SimpleNamespace(state=SimpleNamespace(sessions=cold))
    import clio_agent.gact.transcript_file as transcript_file

    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(transcript_file, "has_durable_transcript_store", lambda _app: True)
        restart_interruptions.mark_restart_interrupted(app)
    assert backing.row_reads == [f"row/{ids[2]}"]
    assert cold.get(ids[2]).status == "error"


def _idle(store: SessionStore, sid: str, when: datetime) -> None:
    """Back-date ``sid``'s last interaction and persist its record and columns."""
    store.get(sid).last_interaction_at = when.strftime("%Y-%m-%dT%H:%M:%SZ")  # noqa: SLF001
    store.update(sid, metadata_patch={"touched": True})


def test_a_memory_tick_reads_only_the_records_of_due_sessions() -> None:
    """In flight and due are read; busy, fresh, delegated and settled are counted."""
    from clio_agent.gact.memory import background
    from clio_agent.gact.memory.tick_index import tick_sessions

    backing = _CountingStore()
    store = SessionStore(store=backing)
    now = datetime.now(UTC)
    old = now - timedelta(hours=2)
    make = store.create
    due, busy, fresh, settled, in_flight = (make(workspace_id="ws").id for _ in range(5))
    agent = make(workspace_id="ws", metadata={"session_type": "agent_task"}).id
    for sid in (due, busy, settled, agent, in_flight):
        _idle(store, sid, old)
    store.update(busy, status="running")
    store.update(in_flight, metadata_patch={background.IN_FLIGHT: {"handle": "h"}})
    verdict = background._checked(store.get(settled), "too_few_new_messages", 3)  # noqa: SLF001
    store.update(settled, metadata_patch={background.CHECKED: verdict})
    store.flush()
    cold = _cold(backing)
    app = SimpleNamespace(state=SimpleNamespace(sessions=cold))

    rows, counted = tick_sessions(app, now, timedelta(minutes=30), 3)

    assert {row.id for row in rows} == {due, in_flight}
    assert sorted(backing.row_reads) == sorted(f"row/{sid}" for sid in (due, in_flight))
    assert counted == {
        "session_busy": 1,
        "not_idle_yet": 1,
        "not_a_conversation": 1,
        "too_few_new_messages": 1,
    }
    assert f"row/{fresh}" not in backing.row_reads  # not idle yet: counted, not read
    # A changed minimum unsettles the verdict: the session is read again.
    backing.row_reads.clear()
    rows, _counted = tick_sessions(app, now, timedelta(minutes=30), 1)
    assert settled in {row.id for row in rows}


def test_autonomy_controls_read_only_sessions_with_an_active_goal_or_loop(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from clio_agent.gact import autonomy_guard, session_host_agent

    backing = _CountingStore()
    store = SessionStore(store=backing)
    ids = [store.create(workspace_id="ws", title=f"t{i}").id for i in range(6)]
    goal = {"active": True, "phase": "needs_input"}  # waits on a person: left as is
    store.update(ids[4], metadata_patch={"goal": goal})
    store.flush()
    cold = _cold(backing)
    schedules = SimpleNamespace(list=lambda: [], get=lambda _sid: None)
    app = SimpleNamespace(state=SimpleNamespace(sessions=cold, schedules=schedules))

    autonomy_guard.reconcile_autonomous_controls(app)
    assert set(backing.row_reads) == {f"row/{ids[4]}"}

    backing.row_reads.clear()
    monkeypatch.setattr(session_host_agent, "selected_ref", lambda _app, _sid, _req: None)
    assert autonomy_guard._scheduled_host(app) is None  # noqa: SLF001
    assert set(backing.row_reads) <= {f"row/{ids[4]}"}


def test_an_agent_fork_count_reads_only_the_callers_children() -> None:
    from clio_agent.gact.agent_fork import _agent_forks
    from clio_agent.gact.conversation_origin import FORK_ORIGIN_KEY

    backing = _CountingStore()
    store = SessionStore(store=backing)
    parent = store.create(workspace_id="ws", title="root").id
    others = [store.create(workspace_id="ws", title=f"o{i}").id for i in range(4)]
    by_agent = {"author": {"kind": "agent"}, "invocation": "call-1"}
    agent_fork = store.create(
        workspace_id="ws", parent_session_id=parent, metadata={FORK_ORIGIN_KEY: by_agent}
    ).id
    person_fork = store.create(
        workspace_id="ws",
        parent_session_id=parent,
        metadata={FORK_ORIGIN_KEY: {"author": {"kind": "person"}}},
    ).id
    store.flush()
    cold = _cold(backing)
    app = SimpleNamespace(state=SimpleNamespace(sessions=cold))

    forks = _agent_forks(app, parent)

    assert [row.id for row in forks] == [agent_fork]
    assert sorted(backing.row_reads) == sorted(f"row/{sid}" for sid in (agent_fork, person_fork))
    assert not set(backing.row_reads) & {f"row/{sid}" for sid in others}
