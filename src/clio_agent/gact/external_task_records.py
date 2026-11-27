"""Snapshot writes for an external call's attention, progress and rejection.

Split out of :mod:`clio_agent.gact.external_tasks`, which keeps thin delegates so its
callers and tests reach the same methods.
"""

from __future__ import annotations

import asyncio
import copy
import logging
import threading
from collections.abc import Callable
from typing import Any
from uuid import uuid4

from clio_agent.arc.task_types import TaskProgress
from clio_agent.gact.native_work import now
from clio_agent.gact.task_publication import publish_task_notice
from clio_agent.runtime import net_refusals

logger = logging.getLogger(__name__)


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


def refusal_keeper(history: Any, loop: asyncio.AbstractEventLoop) -> Callable[[str], None]:
    """A :func:`clio_agent.runtime.net_refusals.outlast` ``keep``: persist the task's
    refusal notice with its record, so a restart before it settles does not lose it.

    The chokepoint calls it on its own thread for each refusal; it only schedules one
    write per task onto the app loop, which reads the notice when it runs.
    """
    pending: set[str] = set()
    lock = threading.Lock()
    writes: set[asyncio.Task[None]] = set()

    async def write(handle: str) -> None:
        with lock:
            pending.discard(handle)
        try:
            await history.io.run(keep_refusals, history, handle)
        except Exception:  # noqa: BLE001 — the live notice still reaches the settled result
            logger.warning(
                "task refusal notice not kept reason=write_failed task=%s", handle, exc_info=True
            )

    def schedule(handle: str) -> None:
        task = loop.create_task(write(handle))
        writes.add(task)
        task.add_done_callback(writes.discard)

    def keep(handle: str) -> None:
        with lock:
            if handle in pending:
                return
            pending.add(handle)
        loop.call_soon_threadsafe(schedule, handle)

    return keep


def keep_refusals(history: Any, handle: str) -> None:
    """Write the refusals told to ``handle``'s call so far into its task details."""
    text = net_refusals.settled_notice(handle)
    if not text:
        return
    with history.serialized():
        current = history.task_snapshot(handle)
        if current is None or current.terminal_at is not None:
            return
        details = copy.deepcopy(history.read_content(current.details)) if current.details else {}
        if details.get("network_refused") == text:
            return
        details["network_refused"] = text
        reference = history.write_content(current.owner.session_id, details)
        history.record_snapshot(
            current.model_copy(update={"details": reference, "updated_at": now()}),
            observation_id=uuid4().hex,
        )


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
