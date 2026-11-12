"""Deleting a stopped conversation settles the assignment Stop held for it.

Stop keeps the conversation's assignment open (activity ``stopped``) so the next message
can continue it; the delete guard then refused every stopped conversation with 409
``session_has_active_work``. Delete now cancels held assignments itself, and only when
nothing else in the conversation's tree is active and no turn runs.
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from typing import Any

import pytest

from clio_agent.gact import session_delete_settle as settle


def _task(handle: str, sid: str, activity: str, kind: str = "Session") -> Any:
    return SimpleNamespace(
        handle=handle,
        kind=kind,
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
