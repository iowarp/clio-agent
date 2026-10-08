"""Shared controls retain the actual configured relay owner and its cleanup boundary."""

from __future__ import annotations

import asyncio
import threading
import time
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from fastapi.testclient import TestClient

from clio_agent.gact.app import build_app
from clio_agent.gact.mcp_task_store import app_task_store
from clio_agent.gact.task_projection import resolve_task
from clio_agent.tools.mcp_task_records import TaskKey, TaskRecord, resolve_store
from tests.test_gact.test_invoker_s7 import (
    _Agent,
    _declare,
    _FakeRelayBackend,
    _FakeRelayClient,
    _relay_current,
    _relay_invoker,
    _spec,
)


class _HeldBackend(_FakeRelayBackend):
    """A backend whose cancellation ACK and execution settlement are independently gated."""

    def __init__(self, server_id: str = "fake-relay-server") -> None:
        super().__init__(server_id)
        self.release = threading.Event()
        self.cancel_calls = 0

    def client(self, owner_session_id: str) -> _HeldClient:
        return _HeldClient(self, owner_session_id)

    def current(self, task_id: str, *, terminal: bool = False) -> SimpleNamespace:
        del terminal
        task = self.tasks[task_id]
        task["polls"] += 1
        if task["awaiting_input"]:
            return _relay_current("input_required")
        if not self.release.is_set():
            return _relay_current("running")
        state = "canceled" if task["cancel_requested"] else "succeeded"
        task["state"] = state
        task["terminal"].set()
        result = (
            None
            if state == "canceled"
            else {"isError": False, "task_result": self.task_result(task)}
        )
        current = _relay_current(state, result=result)
        store = resolve_store(None)
        record = store.get(self._key(task))
        assert record is not None
        store.put(replace(record, status=current.status, result=result))
        return current


class _HeldClient(_FakeRelayClient):
    async def resume(
        self, key: TaskKey, *, timeout_seconds: float | None = None
    ) -> SimpleNamespace:
        end = None if timeout_seconds is None else time.monotonic() + timeout_seconds
        while not self.backend.release.is_set() and (end is None or time.monotonic() < end):
            await asyncio.sleep(0.01)
        return self.backend.current(key.task_id)

    async def cancel(self, identity: Any) -> dict[str, bool]:
        self.backend.cancel_calls += 1
        return await super().cancel(identity)


def _eventually(predicate: Any, timeout: float = 5.0) -> None:
    end = time.monotonic() + timeout
    while not predicate() and time.monotonic() < end:
        time.sleep(0.02)
    assert predicate()


def test_application_driver_collects_relay_without_parent_wait(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The original task completes without resubmission or an agent wait call."""
    _declare(monkeypatch, "main")
    app = build_app(sessions_path=tmp_path / "s.json", agent=_Agent())
    backend = _HeldBackend()
    with TestClient(app) as client:
        sid = client.post("/v1/sessions", json={"title": "relay owner"}).json()["id"]
        owner = _relay_invoker(app, backend)
        app.state.relay_expert_invokers = {"ares": owner}
        handle = owner.invoke(_spec(sid))
        row = resolve_task(app, sid, handle.handle_id)
        assert row["effective_status"] == "running"
        assert row["connection_freshness"] == "connected"
        assert len(backend.submissions) == 1
        assert handle.task_id in app.state.task_supervisor.drivers
        backend.release.set()
        _eventually(lambda: app.state.agent_task_registry.get(handle.task_id).is_terminal)
        task = app.state.agent_task_registry.get(handle.task_id)
        assert task.status == "completed" and task.notify_pending
        assert task.result["answer_excerpt"].startswith("child did:")
        assert len(backend.submissions) == 1


def test_colliding_relay_ids_have_durable_distinct_public_handles(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Two backend IDs equal within one owner resolve only through their full identities."""
    from clio_agent.gact.agent_tasks import AgentTaskRegistry
    from clio_agent.gact.task_controls import query_snapshot

    _declare(monkeypatch, "main")
    app = build_app(sessions_path=tmp_path / "s.json", agent=_Agent())
    first, second = _HeldBackend("backend-one"), _HeldBackend("backend-two")
    with TestClient(app) as client:
        sid = client.post("/v1/sessions", json={"title": "colliding relay"}).json()["id"]
        owners = [_relay_invoker(app, backend) for backend in (first, second)]
        handles = [owner.invoke(_spec(sid)) for owner in owners]
        assert len({handle.handle_id for handle in handles}) == 2
        keys = [owner._task_key(handle) for owner, handle in zip(owners, handles, strict=True)]
        assert keys[0].task_id == keys[1].task_id
        assert keys[0].server_id != keys[1].server_id
        for handle, key in zip(handles, keys, strict=True):
            saved = app.state.sessions.get(handle.child_session_id).metadata["relay_task_key"]
            assert saved == key.to_wire()
            assert handle.handle_id != key.task_id
            from clio_agent.gact.relay_timeline import (
                relay_timeline_view,
                route_relay_timeline_event,
            )

            assert route_relay_timeline_event(
                app,
                handle,
                {
                    "task_id": key.task_id,
                    "seq": 1,
                    "event_type": "progress",
                    "summary": key.server_id,
                },
            )
            rows, _ = relay_timeline_view(app, handle.task_id)
            assert len(rows) == 1 and rows[0]["summary"] == key.server_id
        snapshot = query_snapshot(app, sid, kind="Subagent")
        assert snapshot["total"] == 2
        with pytest.raises(ValueError, match="ambiguous_task"):
            resolve_task(app, sid, keys[0].task_id)
        rebuilt = AgentTaskRegistry()
        rebuilt.rebuild_from_sessions(app.state.sessions.list())
        app.state.agent_task_registry = rebuilt
        for owner, handle, key in zip(owners, handles, keys, strict=True):
            assert owner._task_key(handle) == key
            assert resolve_task(app, sid, handle.handle_id)["description"] == "analyze the dataset"
        for backend in (first, second):
            backend.release.set()
        for owner, handle in zip(owners, handles, strict=True):
            assert owner.wait(handle, timeout_s=5).status == "completed"
        assert [len(backend.submissions) for backend in (first, second)] == [1, 1]


def test_shared_relay_cancel_waits_for_owner_and_descendants(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """ACK, relay terminal and descendant cleanup are three different lifecycle edges."""
    _declare(monkeypatch, "main")
    app = build_app(sessions_path=tmp_path / "s.json", agent=_Agent())
    backend = _HeldBackend()
    with TestClient(app) as client:
        sid = client.post("/v1/sessions", json={"title": "relay cancel"}).json()["id"]
        owner = _relay_invoker(app, backend)
        app.state.relay_expert_invokers = {"ares": owner}
        handle = owner.invoke(_spec(sid))
        store = app_task_store(app)
        descendant = TaskRecord(
            key=TaskKey("descendant", handle.child_session_id, "held"),
            kind="Download",
            handle="owned-descendant",
            status="working",
        )
        store.put(descendant)
        descendant_ack = threading.Event()

        async def cancel_descendant() -> bool:
            descendant_ack.set()
            return True

        app.state.task_supervisor.cancellers[descendant.handle] = cancel_descendant
        prefix = f"/v1/sessions/{sid}/async-tasks"
        for _ in range(2):
            result = client.post(prefix + "/cancel", json={"tasks": handle.handle_id})
            assert result.status_code == 200, result.text
            assert result.json()["errors"] == []
        assert backend.cancel_calls == 1
        assert descendant_ack.wait(1.0)
        assert app.state.agent_task_registry.get(handle.task_id).cancel_requested
        assert app.state.sessions.get(handle.child_session_id).metadata["task_admission_closed"]
        assert handle.child_session_id not in app.state.cancel_flags
        backend.release.set()
        _eventually(lambda: backend.tasks[owner._task_key(handle).task_id]["terminal"].is_set())
        owner.check([handle])
        assert not app.state.agent_task_registry.get(handle.task_id).is_terminal
        assert resolve_task(app, sid, handle.handle_id)["raw_status"] == "cancelled"
        store.put(replace(descendant, status="cancelled", effective_status="cancelled"))
        _eventually(lambda: app.state.agent_task_registry.get(handle.task_id).is_terminal)
        assert app.state.agent_task_registry.get(handle.task_id).status == "cancelled"
        assert len(backend.submissions) == 1
