"""SSE event publication for durable MCP task records (#1205, #1236).

``gact/mcp_task_events.py`` had ZERO direct test coverage before this file --
every existing assertion on its behavior lived one layer up, driven through
``TaskRecordStore.put`` in ``test_async_processes_1205.py``. This file tests
the module's own two publish functions directly (the exact event TYPE/shape
decision), plus the #1236 addition: a SEPARATE lean ``mcp_task.console`` delta
event fed by the console-listener hook, installed alongside the existing
full-record change listener by ``install_mcp_task_event_publisher``.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from clio_agent.gact.app import build_app
from clio_agent.gact.events import EventBus
from clio_agent.gact.mcp_task_events import (
    MCP_TASK_CONSOLE_EVENT,
    publish_mcp_task_console_delta,
    publish_mcp_task_event,
)
from clio_agent.tools.mcp_task_records import (
    TaskKey,
    TaskRecord,
    set_task_change_listener,
    set_task_console_listener,
    task_change_listener,
    task_console_listener,
)


class _Agent:
    def forward(self, question: str, session_id: str, **_kwargs: Any) -> Any:
        return type("P", (), {"answer": "ok", "selected_expert": "", "routing_rationale": ""})()


def _build(tmp_path: Path):
    return build_app(sessions_path=tmp_path / "sessions.json", agent=_Agent())


class _FakeApp:
    """A bare ``app.state.bus`` stand-in -- these publish functions touch
    nothing else on ``app``, so a real FastAPI instance is unnecessary noise
    for the unit-level shape assertions below."""

    class _State:
        pass

    def __init__(self) -> None:
        self.state = self._State()
        self.state.bus = EventBus()


@pytest.fixture(autouse=True)
def _isolate_process_hooks() -> Any:
    """Never leak one test's installed listeners into another's process-global."""

    yield
    set_task_change_listener(None)
    set_task_console_listener(None)


# --------------------------------------------------------------------------- #
# publish_mcp_task_event: event TYPE follows display_status (#1236), never    #
# the raw wire status -- a delivered-error "completed" must publish failed.   #
# --------------------------------------------------------------------------- #


def _key(task_id: str = "task-1", session_id: str | None = "sess-1") -> TaskKey:
    return TaskKey(server_id="relay-ares", session_id=session_id, task_id=task_id)


def test_publish_mcp_task_event_types_by_raw_status_when_no_effective_status_derived() -> None:
    """A record no poll has touched (or one persisted before #1236) has an
    empty ``effective_status`` -- ``display_status`` falls back to the raw
    ``status``, matching the pre-#1236 behavior byte-for-byte."""

    app = _FakeApp()
    record = TaskRecord(key=_key(), tool="jarvis_run", status="completed")

    publish_mcp_task_event(app, record)  # type: ignore[arg-type]

    events = app.state.bus.session_events_since("sess-1")
    assert [e.type for e in events] == ["mcp_task.completed"]


def test_publish_mcp_task_event_types_by_effective_status_over_raw_status() -> None:
    """The #1236 headline: a record whose raw wire status is "completed" but
    whose effective_status was derived to "failed" (a delivered isError result,
    clio-relay#265) must publish ``mcp_task.failed`` -- never ``.completed``."""

    app = _FakeApp()
    record = TaskRecord(
        key=_key(),
        tool="jarvis_run",
        status="completed",
        effective_status="failed",
        effective_status_reason="exit code 186",
    )

    publish_mcp_task_event(app, record)  # type: ignore[arg-type]

    events = app.state.bus.session_events_since("sess-1")
    assert [e.type for e in events] == ["mcp_task.failed"]
    payload = events[0].payload
    # Nothing destroyed: the raw wire status still rides along, honestly.
    assert payload["status"] == "completed"
    assert payload["effective_status"] == "failed"
    assert payload["effective_status_reason"] == "exit code 186"


def test_publish_mcp_task_event_for_an_unattributed_record_publishes_nothing() -> None:
    app = _FakeApp()
    record = TaskRecord(key=_key(session_id=None), tool="jarvis_run", status="working")

    publish_mcp_task_event(app, record)  # type: ignore[arg-type]

    assert app.state.bus.session_events_since("") == []


# --------------------------------------------------------------------------- #
# publish_mcp_task_console_delta: the #1236 lean event -- delta only, never   #
# a wrapped/full record.                                                     #
# --------------------------------------------------------------------------- #


def test_publish_mcp_task_console_delta_carries_only_the_delta() -> None:
    app = _FakeApp()
    key = _key()

    publish_mcp_task_console_delta(
        app,  # type: ignore[arg-type]
        key,
        channel="console",
        delta="second line\n",
        offset=23,
        truncated=False,
    )

    events = app.state.bus.session_events_since("sess-1")
    assert len(events) == 1
    event = events[0]
    assert event.type == MCP_TASK_CONSOLE_EVENT == "mcp_task.console"
    assert event.payload == {
        "key": key.to_wire(),
        "channel": "console",
        "delta": "second line\n",
        "offset": 23,
        "truncated": False,
    }
    # Lean means lean: no "backend"/"status"/whole-record fields leaked in.
    assert "backend" not in event.payload
    assert "status" not in event.payload


def test_publish_mcp_task_console_delta_channel_is_not_hardcoded() -> None:
    """A future relay stderr tail must slot in via ``channel`` alone."""

    app = _FakeApp()
    publish_mcp_task_console_delta(
        app,
        _key(),
        channel="stderr",
        delta="oops\n",
        offset=5,
        truncated=False,  # type: ignore[arg-type]
    )

    event = app.state.bus.session_events_since("sess-1")[0]
    assert event.payload["channel"] == "stderr"


def test_publish_mcp_task_console_delta_for_an_unattributed_key_publishes_nothing() -> None:
    app = _FakeApp()

    publish_mcp_task_console_delta(
        app,  # type: ignore[arg-type]
        _key(session_id=None),
        channel="console",
        delta="x",
        offset=1,
        truncated=False,
    )

    assert app.state.bus.session_events_since("") == []


# --------------------------------------------------------------------------- #
# install_mcp_task_event_publisher: wires BOTH hooks, end to end through a    #
# real build_app -- the actual production boot path (gact/app.py calls this   #
# exact function).                                                            #
# --------------------------------------------------------------------------- #


def test_install_wires_both_the_change_listener_and_the_console_listener(
    tmp_path: Path,
) -> None:
    app = _build(tmp_path)
    with TestClient(app):
        assert task_change_listener() is not None
        assert task_console_listener() is not None


def test_console_listener_installed_by_a_real_boot_publishes_a_lean_event(
    tmp_path: Path,
) -> None:
    """Drives the installed hook exactly the way ``relay_console.py``'s
    ``_on_poll`` calls it (positional: key, channel, delta, offset, truncated)
    against a REAL app/bus from ``build_app`` -- the actual boot-time wiring,
    not a hand-rolled stand-in."""

    app = _build(tmp_path)
    with TestClient(app) as client:
        sid = client.post("/v1/sessions", json={"title": "parent"}).json()["id"]
        key = TaskKey(server_id="relay-ares", session_id=sid, task_id="jarvis-console")

        listener = task_console_listener()
        assert listener is not None
        listener(key, "console", "new bytes\n", 42, False)

        events = app.state.bus.session_events_since(sid, cursor=1)
        console_events = [e for e in events if e.type == "mcp_task.console"]

    assert len(console_events) == 1
    assert console_events[0].payload["delta"] == "new bytes\n"
    assert console_events[0].payload["offset"] == 42


async def test_a_real_console_fold_publishes_both_the_snapshot_and_the_lean_delta_event(
    tmp_path: Path,
) -> None:
    """End-to-end proof for #1236's headline claim: driving relay's console
    fold (``tools/relay_console.py::make_console_on_poll``) against the REAL
    production store (``SessionMetadataTaskStore``, installed by
    ``build_app``) publishes BOTH the existing full-record ``mcp_task.updated``
    snapshot (unchanged, #1205) AND the new lean ``mcp_task.console`` delta
    event on the SAME session channel -- the missing SSE fan-out this issue
    closes.

    Three ``mcp_task.updated`` snapshots are expected, not two: the initial
    ``store.put`` below fires its own (status="working", no console yet)
    BEFORE either ``on_poll`` round runs, then each of the two rounds' own
    ``store.put`` (inside the fold) fires one more. Each round's console
    growth ALSO fires the new lean delta event -- two of those."""

    import httpx

    from clio_agent.tools.mcp_task_records import task_record_store
    from clio_agent.tools.relay_console import make_console_on_poll

    class _FakeRelayHttpClient:
        def __init__(self, chunks: list[tuple[str, int]]) -> None:
            self._chunks = chunks
            self._calls = 0

            def handler(request: httpx.Request) -> httpx.Response:
                text, next_offset = self._chunks[self._calls]
                self._calls += 1
                return httpx.Response(
                    200,
                    json={
                        "job_id": "job-1",
                        "stream": "console",
                        "offset": int(request.url.params.get("offset", "0")),
                        "next_offset": next_offset,
                        "eof": False,
                        "text": text,
                    },
                )

            self._client = httpx.AsyncClient(
                transport=httpx.MockTransport(handler), base_url="http://relay.invalid"
            )

        def _require_http_client(self) -> httpx.AsyncClient:
            return self._client

    app = _build(tmp_path)
    with TestClient(app) as client:
        sid = client.post("/v1/sessions", json={"title": "parent"}).json()["id"]
        key = TaskKey(server_id="relay-real", session_id=sid, task_id="jarvis-real-console")
        store = task_record_store()
        store.put(TaskRecord(key=key, tool="jarvis_run", status="working"))

        fake_relay = _FakeRelayHttpClient([("first line\n", 11), ("second line\n", 23)])
        on_poll = make_console_on_poll(fake_relay, "job-1")
        assert on_poll is not None

        class _Current:
            status = "working"

        await on_poll(_Current(), key, store)  # type: ignore[arg-type]
        await on_poll(_Current(), key, store)  # type: ignore[arg-type]

        events = app.state.bus.session_events_since(sid, cursor=1)

    snapshot_events = [e for e in events if e.type == "mcp_task.updated"]
    console_events = [e for e in events if e.type == "mcp_task.console"]

    assert len(snapshot_events) == 3, "the existing full-record fan-out still fires"
    assert snapshot_events[0].payload["status"] == "working"
    assert "console" not in snapshot_events[0].payload["backend"], (
        "the initial put's own snapshot precedes any console fold"
    )
    assert snapshot_events[-1].payload["backend"]["console"]["tail"] == "first line\nsecond line\n"

    assert len(console_events) == 2, "the NEW lean delta event now also fires"
    assert [e.payload["delta"] for e in console_events] == ["first line\n", "second line\n"]
    # Lean: each delta event is much smaller than the full accumulated tail.
    assert len(console_events[-1].payload["delta"]) < len(
        snapshot_events[-1].payload["backend"]["console"]["tail"]
    )
