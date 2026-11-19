"""Snapshot writes for an external call's attention, progress and rejection.

Split out of :mod:`clio_agent.gact.external_tasks`, which keeps thin delegates so its
callers and tests reach the same methods.
"""

from __future__ import annotations

from typing import Any
from uuid import uuid4

from clio_agent.arc.task_types import TaskProgress
from clio_agent.gact.native_work import now
from clio_agent.gact.task_publication import publish_task_notice


def attention(
    history: Any, handle: str, message: str, *, lost: bool = True, episode: str = ""
) -> None:
    """Mark the call's state uncertain and open (or keep) one attention episode."""
    with history.serialized():
        current = history.task_snapshot(handle)
        if current.terminal_at is not None:
            return
        if (
            current.status == "task_state_uncertain"
            and current.progress.message == message
            and (not lost or current.connection_freshness == "lost")
            and current.attention_episode
        ):
            return
        observed = current.model_copy(
            update={
                "status": "task_state_uncertain",
                "settlement": "unknown",
                "connection_freshness": "lost" if lost else current.connection_freshness,
                "updated_at": now(),
                "progress": TaskProgress(message=message),
                "attention_episode": current.attention_episode or episode or uuid4().hex,
            }
        )
        history.record_snapshot(observed, observation_id=uuid4().hex)
        publish_task_notice(history, observed, episode=observed.attention_episode)


def record_progress(
    history: Any, handle: str, done: float, total: float | None, message: str | None
) -> None:
    """Persist this invocation's progress; progress alone cannot wake a model."""
    with history.serialized():
        current = history.task_snapshot(handle)
        if current is None or current.terminal_at is not None:
            return
        progress = current.progress.model_copy(
            update={"completed": done, "total": total, "message": message or ""}
        )
        progress = TaskProgress.model_validate(progress.model_dump())
        history.record_snapshot(
            current.model_copy(update={"progress": progress, "updated_at": now()}),
            observation_id=uuid4().hex,
        )


def rejected(history: Any, handle: str, reason: str) -> None:
    """Settle a call the server refused before it started."""
    with history.serialized():
        current = history.task_snapshot(handle)
        if current.terminal_at is not None:
            return
        result = history.write_content(
            current.owner.session_id, {"error": reason, "acceptance": "rejected"}
        )
        stamp = now()
        history.record_snapshot(
            current.model_copy(
                update={
                    "status": "failed",
                    "raw_status": "rejected",
                    "activity": "",
                    "settlement": "never_started",
                    "terminal_at": stamp,
                    "updated_at": stamp,
                    "result": result,
                    "supported_actions": ("get_result",),
                }
            ),
            observation_id=uuid4().hex,
        )
