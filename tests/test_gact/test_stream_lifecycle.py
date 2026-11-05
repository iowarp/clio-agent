"""A stream's life past the happy path: failed and concurrent flushes, reopening, leases.

The REAL app and stream store; only the version write is replaced where a test needs it
to fail or to be slow.
"""

from __future__ import annotations

import threading
import time
from collections.abc import Iterator
from pathlib import Path
from typing import Any, cast

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from clio_agent.gact import context
from clio_agent.gact.app import build_app
from clio_agent.gact.artifacts.registry import get_registry
from clio_agent.gact.streams import persist, push
from clio_agent.gact.streams.store import STREAM_FLUSH_FAILED, StreamError, stream_store
from tests.test_gact.test_document_artifacts import _workspace_session
from tests.test_gact.test_streams import _Bus

pytestmark = pytest.mark.timeout(60)


@pytest.fixture
def world(tmp_path: Path) -> Iterator[tuple[FastAPI, str, str, TestClient, _Bus]]:
    root = tmp_path / "workspace"
    root.mkdir()
    with TestClient(build_app(sessions_path=tmp_path / "sessions.json")) as client:
        workspace_id, session_id = _workspace_session(client, root)
        app = cast(FastAPI, client.app)
        bus, real_bus = _Bus(), app.state.bus
        app.state.bus = bus
        app_token, session_token = context.set_app(app), context.set_session_id(session_id)
        try:
            yield app, workspace_id, session_id, client, bus
        finally:
            context.reset(session_token)
            context.reset(app_token)
            app.state.bus = real_bus


def _versions(app: Any, workspace_id: str, name: str) -> int:
    record = get_registry(app).get(workspace_id, f"{name}.parquet")
    return 0 if record is None else len(record.versions)


def _failing(monkeypatch: pytest.MonkeyPatch, times: int) -> list[int]:
    """The next ``times`` version writes fail with an OSError; the count of failures."""
    real, failed = persist.write_version, [0]

    def write(*args: Any) -> str:
        if failed[0] < times:
            failed[0] += 1
            raise OSError("disk full")
        return real(*args)

    monkeypatch.setattr(persist, "write_version", write)
    return failed


def test_a_failed_flush_keeps_the_rows_for_the_next_flush(
    world: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """SABOTAGE: mark the rows saved before the write (``flushed_seq`` first) -> the next
    flush thinks it is up to date and writes nothing -> red."""
    app, workspace_id, sid, *_ = world
    store = stream_store(app)
    stream = store.open(sid, workspace_id, "gauge", flush_s=0)
    store.write(stream.id, stream.token, [{"v": 1}])
    _failing(monkeypatch, 1)
    with pytest.raises(OSError):
        store.flush(stream)
    error = stream.describe()["flush_error"]
    assert error["reason"] == STREAM_FLUSH_FAILED and error["code"] == "OSError"
    assert _versions(app, workspace_id, "gauge") == 1  # only the open's version
    store.flush(stream)
    assert _versions(app, workspace_id, "gauge") == 2  # the rows were written after all
    assert stream.describe()["flush_error"] is None


def test_a_stop_whose_flush_fails_still_stops_and_is_announced(
    world: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A stop, a script's end and shutdown never raise for a failed version write; the
    views still learn the stream stopped.

    SABOTAGE: let ``_flush_recorded`` raise -> stop raises before announcing -> red."""
    app, workspace_id, sid, _client, bus = world
    store = stream_store(app)
    first = store.open(sid, workspace_id, "first", task="task_1")
    second = store.open(sid, workspace_id, "second")
    push.watch(first.id, sid)
    store.write(first.id, first.token, [{"v": 1}])
    store.write(second.id, second.token, [{"v": 1}])
    _failing(monkeypatch, 2)
    assert store.stop_task("task_1") == 1
    assert first.state == "stopped" and first.describe()["flush_error"]["code"] == "OSError"
    stops = [e for e in bus.events if e.payload.get("state") == "stopped"]
    assert [e.payload["stream_id"] for e in stops] == [first.id]
    assert first.id not in push._WATCHES  # a stopped stream's watch is forgotten
    store.close()  # shutdown: the other stream stops too, its failure recorded
    assert second.state == "stopped" and second.describe()["flush_error"] is not None


def test_the_flush_timer_saves_changes_and_retries_a_failed_write(
    world: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """SABOTAGE: do not reschedule after a failed timer flush -> the rows stay unsaved
    until the stream stops -> no second version -> red."""
    app, workspace_id, sid, *_ = world
    store = stream_store(app)
    stream = store.open(sid, workspace_id, "ticker", flush_s=0.2)
    failed = _failing(monkeypatch, 1)
    store.write(stream.id, stream.token, [{"v": 1}])
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline and (
        _versions(app, workspace_id, "ticker") < 2 or stream.describe()["flush_error"]
    ):
        time.sleep(0.05)
    assert failed[0] == 1 and _versions(app, workspace_id, "ticker") == 2
    assert stream.describe()["flush_error"] is None
    store.stop(stream.id, stream.token)


def test_concurrent_flushes_of_one_stream_never_overlap(
    world: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The timer, a stop and a dashboard's publish all flush the same file.

    SABOTAGE: drop ``flush_lock`` -> two writes of the same parquet path overlap -> red."""
    app, workspace_id, sid, *_ = world
    store = stream_store(app)
    stream = store.open(sid, workspace_id, "shared", flush_s=0)
    active, overlaps = [0], [0]
    guard = threading.Lock()

    def slow(*_args: Any) -> str:
        with guard:
            active[0] += 1
            overlaps[0] += active[0] > 1
        time.sleep(0.05)
        with guard:
            active[0] -= 1
        return "artifact_x"

    monkeypatch.setattr(persist, "write_version", slow)

    def writer(n: int) -> None:
        store.write(stream.id, stream.token, [{"n": n}])
        store.flush(stream)

    threads = [threading.Thread(target=writer, args=(n,)) for n in range(4)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert overlaps[0] == 0


def test_another_session_cannot_open_an_open_stream_and_a_reopen_gets_a_new_key(
    world: Any,
) -> None:
    """SABOTAGE: keep the old key on a reopen after stop -> the stopped writer still
    writes -> red."""
    app, workspace_id, sid, *_ = world
    store = stream_store(app)
    stream = store.open(sid, workspace_id, "kiln")
    old_key = stream.token
    with pytest.raises(StreamError) as busy:
        store.open("sess_other", workspace_id, "kiln")
    assert busy.value.code == "stream_in_use"
    store.stop(stream.id, old_key)
    reopened = store.open("sess_other", workspace_id, "kiln")
    assert reopened is stream and stream.session_id == "sess_other"
    with pytest.raises(StreamError) as stale:
        store.write(stream.id, old_key, [{"x": 1}])
    assert stale.value.code == "stream_not_open"
    store.write(stream.id, stream.token, [{"x": 1}])


def test_reopening_an_open_stream_keeps_its_script_as_owner(world: Any) -> None:
    """A model's ``open_stream`` of a name a script writes does not take it over.

    SABOTAGE: set ``task`` on every open -> the script's end no longer stops it -> red."""
    app, workspace_id, sid, *_ = world
    store = stream_store(app)
    stream = store.open(sid, workspace_id, "feed", task="task_script")
    key = stream.token
    store.open(sid, workspace_id, "feed")  # the model reads its view_uri
    assert stream.task == "task_script" and stream.token == key
    assert store.stop_task("task_script") == 1 and stream.state == "stopped"


def test_a_failed_open_leaves_no_stream_behind(world: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    """SABOTAGE: register the stream and let the flush raise -> a ghost open stream that
    refuses every other session -> red."""
    app, workspace_id, sid, *_ = world
    _failing(monkeypatch, 1)
    store = stream_store(app)
    with pytest.raises(StreamError) as refused:
        store.open(sid, workspace_id, "ghost")
    assert refused.value.code == STREAM_FLUSH_FAILED
    assert store.by_name(workspace_id, "ghost") is None
    assert store.open("sess_other", workspace_id, "ghost").session_id == "sess_other"


def test_a_window_past_what_a_view_reads_is_refused(
    world: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    app, workspace_id, sid, *_ = world
    monkeypatch.setenv("CLIO_ARTIFACTS_TABLE_QUERY_MAX_ROWS", "100")
    with pytest.raises(StreamError) as refused:
        stream_store(app).open(sid, workspace_id, "big", window=101)
    assert refused.value.code == "stream_window_too_large"
    assert stream_store(app).open(sid, workspace_id, "big", window=100).window == 100


def test_one_views_release_leaves_the_other_views_lease(world: Any) -> None:
    """Two views of one session each hold a lease; one goes, the other still gets pushes.

    SABOTAGE: key the leases by session alone -> the first release drops both -> red."""
    app, workspace_id, sid, client, bus = world
    store = stream_store(app)
    stream = store.open(sid, workspace_id, "dual")
    path = f"/v1/streams/{stream.id}/watch"
    for lease in ("view_a", "view_b"):
        assert client.post(path, json={"session_id": sid, "lease_id": lease}).status_code == 200
    released = client.post(path, json={"session_id": sid, "lease_id": "view_a", "release": True})
    assert released.json()["watchers"] == 1
    store.write(stream.id, stream.token, [{"n": 1}])
    deadline = time.monotonic() + 3
    while time.monotonic() < deadline and not bus.events:
        time.sleep(0.02)
    assert [e.session_id for e in bus.events] == [sid]
    client.post(path, json={"session_id": sid, "lease_id": "view_b", "release": True})
    assert stream.id not in push._WATCHES  # nobody watches: nothing kept


def test_a_watch_names_a_session_of_the_streams_workspace(world: Any, tmp_path: Path) -> None:
    """SABOTAGE: drop the session checks -> any id holds a lease -> red."""
    app, workspace_id, sid, client, _bus = world
    stream = stream_store(app).open(sid, workspace_id, "scoped")
    path = f"/v1/streams/{stream.id}/watch"
    missing = client.post(path, json={"session_id": "sess_nobody"})
    assert missing.status_code == 404
    assert missing.json()["error"]["error"] == "session_not_found"
    other_root = tmp_path / "other"
    other_root.mkdir()
    _other_workspace, other_session = _workspace_session(client, other_root)
    foreign = client.post(path, json={"session_id": other_session})
    assert foreign.status_code == 403
    assert foreign.json()["error"]["error"] == "stream_not_in_session_workspace"


def test_a_deleted_sessions_streams_stop_and_are_forgotten(world: Any) -> None:
    """SABOTAGE: leave the stopped streams registered -> the store grows with every
    deleted session -> red."""
    app, workspace_id, sid, *_ = world
    store = stream_store(app)
    stream = store.open(sid, workspace_id, "orphan")
    push.watch(stream.id, sid)
    assert store.stop_session(sid) == 1
    assert stream.state == "stopped" and store.get(stream.id) is None
    assert store.by_name(workspace_id, "orphan") is None and stream.id not in push._WATCHES
