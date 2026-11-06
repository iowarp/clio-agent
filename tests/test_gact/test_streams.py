"""Streams in clio-core (CT44).

A stream is a ``stream`` artifact whose entries live in clio-core (custody ``clio-core``,
no file): written by a script through the streaming plugin's ``stream`` binding after one
``open_stream`` call, read by the agent, by scripts and by views as it is, and announced
to the views watching it. The REAL app and the suite's clio-core; nothing replaced.
"""

from __future__ import annotations

import asyncio
import hashlib
import time
from collections.abc import Iterator
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast

import dspy
import pytest
from clio_schemas import ArtifactKind, Custody
from fastapi import FastAPI
from fastapi.testclient import TestClient

from clio_agent.gact import context
from clio_agent.gact.agents.tool_settings import apply_tool_settings
from clio_agent.gact.app import build_app
from clio_agent.gact.artifacts.registry import get_registry
from clio_agent.gact.code_mode import kernels
from clio_agent.gact.code_mode import tool as tool_module
from clio_agent.gact.streams import push
from clio_agent.gact.streams.store import (
    JSON_TYPE,
    TEXT_TYPE,
    WRITER_ENDED,
    StreamError,
    StreamStore,
    stream_store,
)
from clio_agent.gact.streams.tools import open_stream, read_stream, stop_stream
from clio_agent.plugins import kernel_bindings
from clio_agent.tools.execution import tool_workspace_context
from tests.test_gact.test_document_artifacts import _workspace_session

pytestmark = pytest.mark.timeout(120)  # one test runs a real kernel

PLUGIN = Path(__file__).resolve().parents[2] / "src/clio_agent/plugins/builtin/streaming"
PNG = b"\x89PNG\r\n\x1a\n" + bytes(range(256)) * 4


class _Bus:
    def __init__(self) -> None:
        self.events: list[Any] = []

    def publish(self, event: Any) -> None:
        self.events.append(event)


@pytest.fixture
def session(tmp_path: Path) -> Iterator[tuple[FastAPI, str, str, TestClient, _Bus, Path]]:
    root = tmp_path / "workspace"
    root.mkdir()
    with TestClient(build_app(sessions_path=tmp_path / "sessions.json")) as client:
        workspace_id, session_id = _workspace_session(client, root)
        app = cast(FastAPI, client.app)
        bus, real_bus = _Bus(), app.state.bus
        app.state.bus = bus
        app_token, session_token = context.set_app(app), context.set_session_id(session_id)
        try:
            yield app, workspace_id, session_id, client, bus, root
        finally:
            context.reset(session_token)
            context.reset(app_token)
            app.state.bus = real_bus


def test_a_stream_is_a_clio_core_artifact_and_keeps_typed_entries(session: Any) -> None:
    """JSON, text and binary entries are stored in clio-core and read back as written;
    the window keeps the newest, oldest first.

    SABOTAGE: write the entries to a file instead of clio-core -> the artifact has a path
    and a new store instance reads nothing -> red."""
    app, workspace_id, sid, _client, _bus, root = session
    store = stream_store(app)
    stream = store.open(sid, workspace_id, "sensor", window=3)
    for n in range(5):
        store.write(stream.id, stream.token, [{"n": n, "temperature_c": 20 + n}])
    entries = store.read(stream)
    assert [e["value"]["n"] for e in entries] == [2, 3, 4]
    assert {e["media_type"] for e in entries} == {JSON_TYPE}
    store.write(stream.id, stream.token, ["door opened"])
    store.write(stream.id, stream.token, [PNG], media_type="image/png")
    newest = store.read(stream, limit=2)
    assert newest[0] == {**newest[0], "media_type": TEXT_TYPE, "text": "door opened"}
    assert newest[1]["media_type"] == "image/png" and "value" not in newest[1]
    assert store.entry(stream, stream.seq) == ("image/png", PNG)  # byte-exact
    record = get_registry(app).get(workspace_id, "sensor")
    version = record.versions[-1]
    assert version.kind == ArtifactKind.STREAM and version.custody == Custody.CLIO_CORE
    assert not version.path and (version.producer or {}).get("stream_id") == stream.id
    assert not any(p.name == "streams" for p in root.rglob("streams"))  # no file anywhere
    restarted = StreamStore(app)  # a fresh process reading clio-core
    again = restarted.by_name(workspace_id, "sensor")
    assert again is not None and again.seq == 7
    assert [e["seq"] for e in restarted.read(again)] == [5, 6, 7]


def test_a_stream_left_open_by_an_earlier_run_reads_as_closed(session: Any) -> None:
    """SABOTAGE: skip closing orphans -> the stream reads open with no writer -> red."""
    app, workspace_id, sid, *_ = session
    stream = stream_store(app).open(sid, workspace_id, "printer")
    stream_store(app).write(stream.id, stream.token, [{"state": "printing"}])
    restarted = StreamStore(app)
    again = restarted.by_name(workspace_id, "printer")
    assert again is not None
    assert again.state == "closed" and again.closed_reason == WRITER_ENDED
    assert restarted.read(again)[-1]["value"] == {"state": "printing"}


def test_writes_need_the_open_s_key_and_a_stream_has_one_writing_session(
    session: Any, tmp_path: Path
) -> None:
    app, workspace_id, sid, client, *_ = session
    store = stream_store(app)
    stream = store.open(sid, workspace_id, "probe", task="task_1")
    with pytest.raises(StreamError) as wrong:
        store.write(stream.id, "not-the-key", [{"x": 1}])
    assert wrong.value.code == "stream_not_open"
    with pytest.raises(StreamError) as busy:
        store.open("sess_other", workspace_id, "probe")
    assert busy.value.code == "stream_in_use"
    assert store.open(sid, workspace_id, "probe").task == "task_1"  # reopen keeps the owner
    old_key = stream.token
    store.stop(stream.id, session_id=sid)
    with pytest.raises(StreamError):
        store.write(stream.id, old_key, [{"x": 2}])
    reopened = store.open(sid, workspace_id, "probe")
    assert reopened.state == "open" and reopened.token != old_key


def test_only_a_watched_stream_announces_and_a_watcher_gets_the_latest(
    session: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """SABOTAGE: announce without checking the leases -> unwatched writes publish -> red."""
    app, workspace_id, sid, _client, bus, _root = session
    monkeypatch.setenv("CLIO_STREAMS_PUSH_INTERVAL_S", "0.3")
    store = stream_store(app)
    stream = store.open(sid, workspace_id, "status")
    for n in range(3):
        store.write(stream.id, stream.token, [{"n": n}])
    assert [e for e in bus.events if e.type == push.STREAM_UPDATED_EVENT] == []
    push.watch(stream.id, "sess_viewer")
    for n in range(3, 10):
        store.write(stream.id, stream.token, [{"n": n}])
    deadline = time.monotonic() + 3
    while time.monotonic() < deadline and not bus.events:
        time.sleep(0.05)
    time.sleep(0.4)
    events = [e for e in bus.events if e.type == push.STREAM_UPDATED_EVENT]
    assert 1 <= len(events) <= 2 and all(e.transient for e in events)
    assert events[-1].payload["writes"] == 10 and events[-1].payload["name"] == "status"
    store.stop(stream.id, session_id=sid)
    assert bus.events[-1].payload["state"] == "closed"  # the close is announced at once


def test_a_view_reads_the_content_as_it_is_and_the_reference_names_the_stream(
    session: Any,
) -> None:
    """SABOTAGE: keep a query pipeline for streams -> no ``content`` route -> red."""
    from clio_agent.gact.artifacts.references import resolve_reference

    app, workspace_id, sid, client, _bus, _root = session
    store = stream_store(app)
    stream = store.open(sid, workspace_id, "readings", window=10)
    store.write(stream.id, stream.token, [{"t": 1, "c": 20.5}, {"t": 2, "c": 21.0}])
    store.write(stream.id, stream.token, [PNG], media_type="image/png")
    body = client.get(f"/v1/streams/{stream.id}/content").json()
    assert body["stream"]["state"] == "open" and body["stream"]["writes"] == 3
    assert [e.get("value") for e in body["entries"][:2]] == [
        {"t": 1, "c": 20.5},
        {"t": 2, "c": 21.0},
    ]
    binary = client.get(body["entries"][2]["entry_path"])
    assert binary.status_code == 200 and binary.content == PNG
    assert binary.headers["content-type"] == "image/png"
    assert client.post(f"/v1/streams/{stream.id}/table-query", json={}).status_code in (404, 405)
    resolved = asyncio.run(
        resolve_reference(app, stream.view_uri, session_workspace_id=workspace_id)
    )
    assert resolved["stream"] == {
        "stream_id": stream.id,
        "content_path": f"/v1/streams/{stream.id}/content",
    }
    refused = client.get(f"/v1/artifacts/{stream.artifact_id}/bytes")
    assert refused.status_code == 409


def test_a_watch_names_a_session_of_the_streams_workspace(session: Any, tmp_path: Path) -> None:
    """SABOTAGE: drop the session checks -> any id holds a lease -> red."""
    app, workspace_id, sid, client, _bus, _root = session
    stream = stream_store(app).open(sid, workspace_id, "scoped")
    path = f"/v1/streams/{stream.id}/watch"
    missing = client.post(path, json={"session_id": "sess_nobody"})
    assert missing.status_code == 404
    other_root = tmp_path / "other"
    other_root.mkdir()
    _other_workspace, other_session = _workspace_session(client, other_root)
    foreign = client.post(path, json={"session_id": other_session})
    assert foreign.status_code == 403
    for lease in ("view_a", "view_b"):
        assert client.post(path, json={"session_id": sid, "lease_id": lease}).status_code == 200
    released = client.post(path, json={"session_id": sid, "lease_id": "view_a", "release": True})
    assert released.json()["watchers"] == 1


def test_a_script_s_streams_and_a_deleted_session_s_close_and_keep_their_entries(
    session: Any,
) -> None:
    app, workspace_id, sid, *_ = session
    store = stream_store(app)
    job = store.open(sid, workspace_id, "job", task="task_9")
    store.write(job.id, job.token, [{"done": 1}])
    assert store.stop_task("task_9") == 1
    closed = store.by_name(workspace_id, "job")
    assert closed.state == "closed" and closed.closed_reason == "script_ended"
    assert store.read(closed)[-1]["value"] == {"done": 1}
    other = store.open(sid, workspace_id, "other")
    assert store.stop_session(sid) == 1
    assert store.by_name(workspace_id, "other").state == "closed"
    assert other.id not in push._WATCHES


def test_the_agent_reads_a_stream_or_lists_them_and_stops_one(session: Any) -> None:
    app, workspace_id, sid, *_ = session
    opened = open_stream("feed", window=5)
    assert opened["ok"] and opened["view_uri"].endswith("/feed@latest")
    stream_store(app).write(opened["stream_id"], opened["write_key"], [{"v": 1}, "line"])
    read = read_stream("feed")
    assert read["state"] == "open" and read["writes"] == 2
    assert read["latest"]["text"] == "line" and read["entries_read"][0]["value"] == {"v": 1}
    listed = read_stream()
    assert [s["name"] for s in listed["streams"]] == ["feed"]
    stopped = stop_stream("feed")
    assert stopped["state"] == "closed" and stopped["closed_at"]
    assert read_stream("missing")["reason"] == "stream_not_found"
    assert sid and workspace_id


@pytest.fixture
def kernel_workspace(tmp_path: Path) -> Iterator[Path]:
    with tool_workspace_context(str(tmp_path)):
        yield tmp_path
    kernels.close_session("streams")


def test_a_script_writes_and_reads_through_the_binding_with_no_call_per_write(
    session: Any, kernel_workspace: Path
) -> None:
    """The binding opens the stream with one ``open_stream`` call; each write (a record,
    text, bytes) is a host call; ``stream.read`` is one ``read_stream`` call.

    SABOTAGE: leave ``_clio_host_calls`` out of ``execute_script``'s reserved calls and
    every write fails as an unknown tool."""

    app, workspace_id, *_ = session
    calls: list[str] = []

    def open_tool(name: str, window: int = 1, media_type: str = "") -> Any:
        calls.append("open_stream")
        return open_stream(name, window=window, media_type=media_type)

    def read_tool(name: str = "", limit: int = 50) -> Any:
        calls.append("read_stream")
        return read_stream(name, limit=limit)

    source = (PLUGIN / "ai.iowarp.clio/kernel/stream.py").read_text()
    agent = SimpleNamespace(
        tools={
            "open_stream": dspy.Tool(open_tool, name="open_stream", desc="open"),
            "read_stream": dspy.Tool(read_tool, name="read_stream", desc="read"),
            "run_code": dspy.Tool(lambda: None, name="run_code", desc="run"),
        },
        _clio_tool_servers=frozenset(),
        _clio_deferred_catalog=None,
    )
    apply_tool_settings(agent, SimpleNamespace(tools=[], mcp_servers={}))
    from clio_agent.plugins import store as plugin_store

    record = {r.id: r for r in plugin_store.builtin_records()}["streaming"]
    resolution = SimpleNamespace(kernel_bindings=kernel_bindings.resolution_rows([record]))
    kernel_bindings.attach(agent, resolution)
    assert set(agent._clio_host_calls) == {"__stream_write__", "__stream_stop__"}
    assert (
        agent._clio_kernel_bindings["stream"]["digest"]
        == hashlib.sha256(source.encode()).hexdigest()
    )
    outcome = tool_module.run_script(
        agent,
        "import asyncio\n"
        "s = await stream.open('kiln', window=5)\n"
        "for n in range(18):\n"
        "    await s.write(n=n, temp=900 + n)\n"
        "await s.write('lid closed')\n"
        f"s.write({PNG!r}, media_type='image/png')\n"  # a forgotten await still sends
        "await asyncio.sleep(0.2)\n"
        "back = await stream.read('kiln', limit=3)\n"
        "print(back['writes'], back['latest']['media_type'], s.view_uri)",
        session_id="streams",
        scope="main",
    )
    assert outcome["status"] == "completed", outcome
    assert calls == ["open_stream", "read_stream"]  # the twenty writes were host calls
    writes, latest_type, uri = outcome["output"].split()
    assert writes == "20" and latest_type == "image/png" and uri.endswith("/kiln@latest")
    store = stream_store(app)
    kiln = store.by_name(workspace_id, "kiln")
    assert store.entry(kiln, 20) == ("image/png", PNG)
    assert [e.get("value", e.get("text")) for e in store.read(kiln, 3)][:2] == [
        {"n": 17, "temp": 917},
        "lid closed",
    ]
