"""P2.14 (#1131): relay timeline routing and live-view SSE rows."""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator
from pathlib import Path
from types import SimpleNamespace
from typing import Any

from fastapi.responses import StreamingResponse
from fastapi.testclient import TestClient

from clio_agent.gact.agent_tasks import STATUS_QUEUED, STATUS_RUNNING, seed_agent_task
from clio_agent.gact.agents.invoker import TaskHandle
from clio_agent.gact.agents.relay_invoker_runtime import RelayEventPump
from clio_agent.gact.app import build_app
from clio_agent.gact.relay_timeline import (
    RelayTimelineProjection,
    relay_timeline_view,
    route_relay_timeline_event,
)
from clio_agent.tools.mcp_task_records import TaskKey


class _Agent:
    """Minimal host agent; these tests drive only task/timeline plumbing."""

    def forward(self, question: str, session_id: str, **_kwargs: object) -> Any:
        return SimpleNamespace(answer="ok", selected_expert="", routing_rationale="")


class _StreamClient:
    """Finite async relay stream used to drive the pump deterministically."""

    def __init__(self, events: list[Any]) -> None:
        self.events = events

    async def __aenter__(self) -> "_StreamClient":
        return self

    async def __aexit__(self, *_args: object) -> None:
        return None

    async def stream_events(self, identity: Any, *, cursor: int = 1) -> AsyncIterator[Any]:
        assert cursor == 1
        for event in self.events:
            yield event


def _running_event(task_id: str) -> dict[str, Any]:
    """Return a lifecycle event matching the retained relay handle."""

    return {
        "task_id": task_id,
        "event_type": "agent.task.started",
        "session_id": "",
        "status": STATUS_RUNNING,
        "payload": {"task_id": task_id, "status": STATUS_RUNNING},
    }


def _row(task_id: str, seq: int, *, event_type: str = "progress") -> dict[str, Any]:
    """Return one application timeline row."""

    return {
        "task_id": task_id,
        "seq": seq,
        "event_type": event_type,
        "source": "jarvis" if event_type == "progress" else "mcp_call",
        "summary": f"row {seq}",
        "payload": {"value": seq},
    }


def _sse_data(frame: bytes) -> dict[str, Any]:
    """Decode the JSON envelope from one SSE frame."""

    data_line = next(line for line in frame.decode().splitlines() if line.startswith("data: "))
    return json.loads(data_line.removeprefix("data: "))


def test_mixed_pump_folds_lifecycle_and_routes_rows_with_typed_drop(tmp_path: Path) -> None:
    """Lifecycle events keep folding; application rows route; unknown lanes are typed."""

    app = build_app(sessions_path=tmp_path / "sessions.json", agent=_Agent())
    with TestClient(app) as client:
        parent = client.post("/v1/sessions", json={"title": "parent"}).json()["id"]
        queued = seed_agent_task(
            app,
            parent_session_id=parent,
            agent_ref={"expert_id": "remote", "requesting_expert_id": "main"},
            status=STATUS_QUEUED,
            task_id="task_relay_timeline",
            placement="relay:ares",
            host="ares",
        )
        handle = TaskHandle.from_task(queued)
        events = [
            _running_event(handle.task_id),
            _row(handle.task_id, 1),
            _row(handle.task_id, 2, event_type="mcp_call.completed"),
            {"task_id": handle.task_id, "seq": 3, "event_type": "opaque.event"},
        ]
        pump = RelayEventPump(app, lambda _sid: _StreamClient(events))

        asyncio.run(
            pump._stream(
                handle,
                TaskKey(server_id="relay", session_id=parent, task_id=handle.task_id),
            )
        )

        assert app.state.agent_task_registry.get(handle.task_id).status == STATUS_RUNNING
        rows, drops = relay_timeline_view(app, handle.task_id)
        assert [row["sequence"] for row in rows] == [1, 2]
        assert [row["event_type"] for row in rows] == ["progress", "mcp_call.completed"]
        assert [drop["reason"] for drop in drops] == ["relay_timeline_unroutable"]


def test_live_door_sse_replays_timeline_rows_in_order(tmp_path: Path) -> None:
    """``Accept: text/event-stream`` on the existing live door emits row frames."""

    app = build_app(sessions_path=tmp_path / "sessions.json", agent=_Agent())
    with TestClient(app) as client:
        parent = client.post("/v1/sessions", json={"title": "parent"}).json()["id"]
        task = seed_agent_task(
            app,
            parent_session_id=parent,
            agent_ref={"expert_id": "remote"},
            status=STATUS_RUNNING,
            task_id="task_live_sse",
            placement="relay:ares",
            host="ares",
        )
        handle = TaskHandle.from_task(task)
        assert route_relay_timeline_event(app, handle, _row(task.task_id, 10)) is True
        assert route_relay_timeline_event(app, handle, _row(task.task_id, 11)) is True

        endpoint = next(
            route.endpoint
            for route in app.routes
            if getattr(route, "path", "") == "/v1/agent-tasks/{task_id}/live"
        )

        async def read_replay() -> tuple[StreamingResponse, list[bytes]]:
            request = SimpleNamespace(headers={"accept": "text/event-stream"})
            response = await endpoint(task.task_id, request)
            assert isinstance(response, StreamingResponse)
            iterator = response.body_iterator
            frames = [await anext(iterator), await anext(iterator)]
            await iterator.aclose()
            return response, frames

        response, frames = asyncio.run(read_replay())
        assert response.media_type == "text/event-stream"
        assert [frame.decode().splitlines()[0] for frame in frames] == [
            "event: timeline_row",
            "event: timeline_row",
        ]
        assert [_sse_data(frame)["payload"]["sequence"] for frame in frames] == [10, 11]

        json_view = client.get(f"/v1/agent-tasks/{task.task_id}/live").json()
        assert [row["sequence"] for row in json_view["timeline_rows"]] == [10, 11]


def test_timeline_wrong_inputs_are_typed_and_ring_evicts_oldest(tmp_path: Path) -> None:
    """Unknown ids and malformed frames are recorded; row retention stays bounded."""

    app = build_app(sessions_path=tmp_path / "sessions.json", agent=_Agent())
    app.state.relay_timeline_projection = RelayTimelineProjection(max_rows=2, max_drops=8)
    with TestClient(app) as client:
        parent = client.post("/v1/sessions", json={"title": "parent"}).json()["id"]
        task = seed_agent_task(
            app,
            parent_session_id=parent,
            agent_ref={"expert_id": "remote"},
            status=STATUS_RUNNING,
            task_id="task_bounded",
            placement="relay:ares",
            host="ares",
        )
        handle = TaskHandle.from_task(task)

        assert route_relay_timeline_event(app, handle, {"task_id": task.task_id}) is False
        assert (
            route_relay_timeline_event(
                app,
                handle,
                {"task_id": "task_unknown", "seq": 1, "event_type": "progress"},
            )
            is False
        )
        for seq in range(1, 4):
            assert route_relay_timeline_event(app, handle, _row(task.task_id, seq)) is True

        rows, drops = relay_timeline_view(app, task.task_id)
        assert [row["sequence"] for row in rows] == [2, 3]
        assert [drop["reason"] for drop in drops] == [
            "relay_timeline_malformed",
            "relay_timeline_unknown_task",
        ]
