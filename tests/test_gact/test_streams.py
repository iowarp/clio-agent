"""Streams (taskable run_code plan, Part 5; CT22, CT25 (5)-(7)).

A stream is a named artifact whose latest rows live in the server's memory, written by a
script through the streaming plugin's ``stream`` binding after one ``open_stream`` call,
read by views from memory and announced to the views watching it, and written to the
artifact store on open, by its flush interval and when it stops.
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
from fastapi import FastAPI
from fastapi.testclient import TestClient

from clio_agent.gact import context
from clio_agent.gact.agents.tool_settings import apply_tool_settings
from clio_agent.gact.app import build_app
from clio_agent.gact.artifacts.registry import get_registry
from clio_agent.gact.code_mode import kernels
from clio_agent.gact.code_mode import tool as tool_module
from clio_agent.gact.streams import push
from clio_agent.gact.streams.store import StreamError, stream_store
from clio_agent.gact.streams.tools import open_stream, read_stream, stop_stream
from clio_agent.plugins import kernel_bindings
from clio_agent.tools.execution import tool_workspace_context
from tests.test_gact.test_document_artifacts import _workspace_session

pytestmark = pytest.mark.timeout(120)  # one test runs a real kernel

PLUGIN = Path(__file__).resolve().parents[2] / "src/clio_agent/plugins/builtin/streaming"


class _Bus:
    def __init__(self) -> None:
        self.events: list[Any] = []

    def publish(self, event: Any) -> None:
        self.events.append(event)


@pytest.fixture
def session(tmp_path: Path) -> Iterator[tuple[FastAPI, str, str, TestClient, _Bus]]:
    root = tmp_path / "workspace"
    root.mkdir()
    with TestClient(build_app(sessions_path=tmp_path / "sessions.json")) as client:
        workspace_id, session_id = _workspace_session(client, root)
        app = cast(FastAPI, client.app)
        bus = _Bus()
        real_bus = app.state.bus
        app.state.bus = bus
        app_token, session_token = context.set_app(app), context.set_session_id(session_id)
        try:
            yield app, workspace_id, session_id, client, bus
        finally:
            context.reset(session_token)
            context.reset(app_token)
            app.state.bus = real_bus


def test_a_stream_is_an_artifact_from_its_open_and_keeps_its_window(session: Any) -> None:
    """SABOTAGE: drop the flush in ``StreamStore.open`` and the name has no version for a
    view to bind to until the first interval."""

    app, workspace_id, _sid, _client, _bus = session
    opened = open_stream("printer", columns=["state", "nozzle_c"], window=3, flush_s=0)
    assert (
        opened["ok"] and opened["view_uri"] == f"artifact://{workspace_id}/printer.parquet@latest"
    )
    record = get_registry(app).get(workspace_id, "printer.parquet")
    assert record is not None and len(record.versions) == 1
    store = stream_store(app)
    for n in range(5):
        store.write(opened["stream_id"], opened["write_key"], [{"state": "printing", "n": n}])
    read = read_stream("printer")
    assert [row["n"] for row in read["rows_read"]] == [2, 3, 4]  # the window keeps the last 3
    assert read["latest"]["n"] == 4 and read["writes"] == 5 and read["state"] == "open"
    stopped = stop_stream("printer")
    assert stopped["state"] == "stopped" and stopped["stopped_at"]
    record = get_registry(app).get(workspace_id, "printer.parquet")
    assert record is not None and len(record.versions) == 2  # its last rows, written at stop
    assert read_stream("printer")["latest"]["n"] == 4  # still readable, marked stopped
    with pytest.raises(StreamError) as late:
        store.write(opened["stream_id"], opened["write_key"], [{"n": 9}])
    assert late.value.code == "stream_stopped"


def test_only_a_script_that_opened_the_stream_writes_to_it(session: Any) -> None:
    app, *_ = session
    opened = open_stream("probe")
    with pytest.raises(StreamError) as wrong:
        stream_store(app).write(opened["stream_id"], "not-the-key", [{"x": 1}])
    assert wrong.value.code == "stream_not_open"


def test_nothing_is_announced_unwatched_and_a_watcher_gets_the_latest(
    session: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """SABOTAGE: announce without checking the leases and the unwatched writes publish."""

    app, _wid, sid, _client, bus = session
    monkeypatch.setenv("CLIO_STREAMS_PUSH_INTERVAL_S", "0.3")
    opened = open_stream("status")
    store = stream_store(app)
    for n in range(3):
        store.write(opened["stream_id"], opened["write_key"], [{"n": n}])
    assert [e for e in bus.events if e.type == push.STREAM_UPDATED_EVENT] == []
    push.watch(opened["stream_id"], "sess_viewer")
    for n in range(3, 10):
        store.write(opened["stream_id"], opened["write_key"], [{"n": n}])
    deadline = time.monotonic() + 3
    while time.monotonic() < deadline and len(bus.events) < 2:
        time.sleep(0.05)
    time.sleep(0.4)
    events = [e for e in bus.events if e.type == push.STREAM_UPDATED_EVENT]
    assert 1 <= len(events) <= 2  # seven writes, announced at most once per interval
    assert all(e.session_id == "sess_viewer" and e.transient for e in events)
    assert events[-1].payload["writes"] == 10  # the latest state
    assert sid != "sess_viewer"


def test_a_view_reads_the_rows_in_memory_and_the_resolution_names_the_stream(
    session: Any,
) -> None:
    from clio_agent.gact.artifacts.references import resolve_reference

    app, workspace_id, _sid, client, _bus = session
    opened = open_stream("readings", window=10)
    store = stream_store(app)
    store.write(
        opened["stream_id"], opened["write_key"], [{"t": 1, "c": 20.5}, {"t": 2, "c": 21.0}]
    )
    response = client.post(
        f"/v1/streams/{opened['stream_id']}/table-query",
        json={"sort": [{"column": "t", "desc": True}]},
    )
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["returnedRows"] == 2 and body["columns"]["t"] == [2.0, 1.0]
    assert body["stream"]["state"] == "open"
    watched = client.post(f"/v1/streams/{opened['stream_id']}/watch", json={"session_id": "s1"})
    assert watched.json()["watchers"] == 1
    resolved = asyncio.run(
        resolve_reference(app, opened["view_uri"], session_workspace_id=workspace_id)
    )
    assert resolved["stream"]["stream_id"] == opened["stream_id"]


def test_a_background_script_s_streams_stop_when_it_ends(session: Any) -> None:
    app, workspace_id, sid, *_ = session
    stream = stream_store(app).open(sid, workspace_id, "job", task="task_9")
    assert stream_store(app).stop_task("task_9") == 1
    assert stream.state == "stopped"


@pytest.fixture
def kernel_workspace(tmp_path: Path) -> Iterator[Path]:
    with tool_workspace_context(str(tmp_path)):
        yield tmp_path
    kernels.close_session("streams")


def test_a_script_writes_through_the_binding_with_one_tool_call(
    session: Any, kernel_workspace: Path
) -> None:
    """The plugin's binding opens the stream with one ``open_stream`` call; each write is
    a host call, not a tool call.

    SABOTAGE: leave ``_clio_host_calls`` out of ``execute_script``'s reserved calls and
    every write fails as an unknown tool."""

    app, *_ = session
    calls: list[str] = []

    def open_tool(
        name: str, columns: list[str] | None = None, window: int = 1, flush_s: float = 30.0
    ) -> Any:
        calls.append(name)
        return open_stream(name, columns=columns, window=window, flush_s=flush_s)

    source = (PLUGIN / "ai.iowarp.clio/kernel/stream.py").read_text()
    agent = SimpleNamespace(
        tools={
            "open_stream": dspy.Tool(open_tool, name="open_stream", desc="open"),
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
        "s = await stream.open('kiln', window=5)\n"
        "for n in range(20):\n"
        "    await s.write(n=n, temp=900 + n)\n"
        "print(s.view_uri)",
        session_id="streams",
        scope="main",
    )
    assert outcome["status"] == "completed", outcome
    assert calls == ["kiln"]  # one tool call; the twenty writes were host calls
    rows = read_stream("kiln")["rows_read"]
    assert [row["n"] for row in rows] == [15, 16, 17, 18, 19]
    assert outcome["output"].strip().endswith("/kiln.parquet@latest")
    assert app is not None
