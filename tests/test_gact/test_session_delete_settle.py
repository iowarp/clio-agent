"""Deleting a stopped conversation settles the assignment Stop held for it.

Stop keeps the conversation's assignment open (activity ``stopped``) so the next message
can continue it; the delete guard then refused every stopped conversation with 409
``session_has_active_work``. Delete now cancels held assignments itself, and only when
nothing else in the conversation's tree is active and no turn runs. A call a restart
left uncertain, which no live connection carries, ends interrupted instead of blocking.
"""

from __future__ import annotations

import asyncio
import contextlib
from types import SimpleNamespace
from typing import Any

import pytest

from clio_agent.gact import session_delete_settle as settle


def _task(handle: str, sid: str, activity: str, kind: str = "Session") -> Any:
    return SimpleNamespace(
        handle=handle,
        kind=kind,
        status="running",
        activity=activity,
        owner=SimpleNamespace(session_id=sid),
        cancellation_requested_at=None,
    )


class _Io:
    async def run(self, function: Any, *args: Any) -> Any:
        return function(*args)


def _app(tasks: dict[str, Any], *, busy: bool = False) -> tuple[Any, list[Any]]:
    waited: list[Any] = []

    async def wait(caller: Any, handles: Any, return_when: str, timeout_s: float) -> dict:
        waited.append(list(handles))
        return {}

    history = SimpleNamespace(
        io=_Io(),
        tree_handles=lambda sid, active_only=False: set(tasks),
        task_snapshot=tasks.get,
    )
    app = SimpleNamespace(
        state=SimpleNamespace(
            task_history=history,
            turn_runner=SimpleNamespace(busy=lambda sid: busy),
            task_operations=SimpleNamespace(wait=wait),
            human_principal="human:test",
        )
    )
    return app, waited


@pytest.fixture
def cancelled(monkeypatch: pytest.MonkeyPatch) -> list[tuple[str, str]]:
    calls: list[tuple[str, str]] = []

    async def intervene(app: Any, handle: str, action: str, **kwargs: Any) -> dict:
        calls.append((handle, action))
        return {}

    monkeypatch.setattr(settle, "intervene", intervene)
    return calls


@pytest.mark.parametrize("activity", ["stopped", "paused"])
def test_a_held_assignment_is_cancelled_and_awaited(
    cancelled: list[tuple[str, str]], activity: str
) -> None:
    app, waited = _app({"task_a": _task("task_a", "sess_1", activity)})
    assert asyncio.run(settle.settle_held_assignments(app, "sess_1")) == ["task_a"]
    assert cancelled == [("task_a", "cancel")]
    assert waited == [["task_a"]]


def test_running_work_in_the_tree_keeps_everything(cancelled: list[tuple[str, str]]) -> None:
    tasks = {
        "task_a": _task("task_a", "sess_1", "stopped"),
        "task_job": _task("task_job", "sess_1", "executing", kind="Shell"),
    }
    app, waited = _app(tasks)
    assert asyncio.run(settle.settle_held_assignments(app, "sess_1")) == []
    assert cancelled == [] and waited == []


def test_a_running_turn_keeps_everything(cancelled: list[tuple[str, str]]) -> None:
    app, waited = _app({"task_a": _task("task_a", "sess_1", "stopped")}, busy=True)
    assert asyncio.run(settle.settle_held_assignments(app, "sess_1")) == []
    assert cancelled == [] and waited == []


def test_a_busy_assignment_is_not_held(cancelled: list[tuple[str, str]]) -> None:
    app, _ = _app({"task_a": _task("task_a", "sess_1", "busy")})
    assert asyncio.run(settle.settle_held_assignments(app, "sess_1")) == []
    assert cancelled == []


class _Snapshot(SimpleNamespace):
    def model_copy(self, update: dict[str, Any]) -> "_Snapshot":
        return _Snapshot(**{**vars(self), **update})


def _lost(handle: str, sid: str) -> Any:
    return _Snapshot(
        handle=handle,
        kind="MCP",
        tool="serve",
        status="task_state_uncertain",
        activity="",
        owner=SimpleNamespace(session_id=sid),
        cancellation_requested_at=None,
        connection_freshness="lost",
        terminal_at=None,
    )


def _with_external(app: Any, tasks: dict[str, Any], bound: tuple[str, ...] = ()) -> None:
    history = app.state.task_history
    history.serialized = contextlib.nullcontext
    history.record_snapshot = lambda snapshot, observation_id: tasks.update(
        {snapshot.handle: snapshot}
    )
    calls = {h: SimpleNamespace(submission=SimpleNamespace(handle=h)) for h in bound}
    app.state.external_tasks = SimpleNamespace(connections={"relay": SimpleNamespace(calls=calls)})


def test_a_lost_external_task_ends_interrupted(
    cancelled: list[tuple[str, str]], monkeypatch: pytest.MonkeyPatch
) -> None:
    tasks = {
        "task_a": _task("task_a", "sess_1", "stopped"),
        "task_serve": _lost("task_serve", "sess_1"),
    }
    app, waited = _app(tasks)
    _with_external(app, tasks)
    finished: list[str] = []
    monkeypatch.setattr(settle, "finish_terminal", lambda owner, handle: finished.append(handle))
    assert asyncio.run(settle.settle_held_assignments(app, "sess_1")) == ["task_a", "task_serve"]
    ended = tasks["task_serve"]
    assert ended.status == "interrupted" and ended.settlement == "unknown"
    assert ended.terminal_at is not None
    assert ended.progress.message.startswith(settle.REASON_UNREACHABLE)
    assert finished == ["task_serve"]
    assert cancelled == [("task_a", "cancel")] and waited == [["task_a"]]


def test_an_external_task_a_connection_carries_keeps_everything(
    cancelled: list[tuple[str, str]],
) -> None:
    tasks = {
        "task_a": _task("task_a", "sess_1", "stopped"),
        "task_serve": _lost("task_serve", "sess_1"),
    }
    app, waited = _app(tasks)
    _with_external(app, tasks, bound=("task_serve",))
    assert asyncio.run(settle.settle_held_assignments(app, "sess_1")) == []
    assert tasks["task_serve"].terminal_at is None
    assert cancelled == [] and waited == []
