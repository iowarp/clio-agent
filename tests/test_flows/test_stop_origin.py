"""Who stopped a run: the recorded request, never the child whose call it cancelled."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from typing import Any

from clio_agent.arc.task_types import AssignmentDetails
from clio_agent.flows import stop


class _IO:
    async def run(self, fn: Any, *args: Any) -> Any:
        return fn(*args)


def _app(rows: dict[str, dict[str, Any]]) -> Any:
    """Assignments by handle: ``parent``, ``requested`` and their recorded details."""

    def snapshot(handle: str) -> Any:
        row = rows.get(handle)
        if row is None:
            return None
        return SimpleNamespace(
            handle=handle,
            status="cancelled" if row.get("requested") else "running",
            cancellation_requested_at="t" if row.get("requested") else None,
            details=handle if row.get("details") else None,
            owner=SimpleNamespace(parent_handle=row.get("parent")),
        )

    history = SimpleNamespace(
        io=_IO(),
        task_snapshot=snapshot,
        read_content=lambda ref: rows[ref]["details"].model_dump(mode="json"),
    )
    return SimpleNamespace(state=SimpleNamespace(task_history=history, conversations=None))


def _ending(app: Any, monkeypatch: Any, ending: tuple[str, dict[str, Any]]) -> Any:
    async def requested(_app: Any, _root: str) -> bool:
        return True

    monkeypatch.setattr(stop, "stop_requested", requested)
    return asyncio.run(stop.stopped_ending(app, "flow", ending))


CHILD_BLAME = (
    "cancelled",
    {
        "reason": "call_cancelled",
        "message": "subagent 'critic' ended cancelled",
        "handle": "critic",
    },
)


def test_a_cascaded_stop_names_the_person_who_stopped_the_parent(monkeypatch: Any) -> None:
    app = _app(
        {
            "flow": {
                "parent": "main",
                "requested": True,
                "details": AssignmentDetails(cancellation_by="parent"),
            },
            "main": {
                "requested": True,
                "details": AssignmentDetails(
                    cancellation_by="person", cancellation_reason="conversation_stop"
                ),
            },
        }
    )
    status, result = _ending(app, monkeypatch, CHILD_BLAME)
    assert status == "cancelled"
    assert result["reason"] == "stopped"
    assert result["stopped_by"] == "person"
    assert result["stop_reason"] == "conversation_stop"
    assert result["stopped_handle"] == "main"
    assert result["message"] == "Stopped from the conversation's Stop."
    # The child's outcome is kept as what the flow saw, not as the run's reason.
    assert result["ended_by"]["reason"] == "call_cancelled"
    assert result["handle"] == "critic"


def test_an_agents_cancel_is_named_as_the_agents(monkeypatch: Any) -> None:
    app = _app({"flow": {"requested": True, "details": AssignmentDetails(cancellation_by="agent")}})
    _, result = _ending(app, monkeypatch, ("gather_failed", {"reason": "gather_failed"}))
    assert result["stopped_by"] == "agent"
    assert result["message"] == "Stopped by an agent's cancellation."
    assert result["ended_by"]["status"] == "gather_failed"


def test_an_unrecorded_origin_says_unknown_after_the_wait(monkeypatch: Any) -> None:
    monkeypatch.setattr(stop, "ORIGIN_WAIT_S", 0.1)
    app = _app({"flow": {"requested": False}})
    _, result = _ending(app, monkeypatch, CHILD_BLAME)
    assert result["stopped_by"] == "unknown"
    assert result["message"] == "Stopped on request."


def test_a_run_nobody_stopped_keeps_its_own_ending(monkeypatch: Any) -> None:
    async def not_requested(_app: Any, _root: str) -> bool:
        return False

    monkeypatch.setattr(stop, "stop_requested", not_requested)
    assert asyncio.run(stop.stopped_ending(_app({}), "flow", CHILD_BLAME)) == CHILD_BLAME
