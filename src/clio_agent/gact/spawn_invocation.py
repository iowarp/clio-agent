"""The invocation id a model tool call gives the work it starts.

A started task (a child agent, a flow, a workflow, an MCP task) is keyed by its
invocation id: ``Conversations.spawn`` derives the handle from it. Native tools run
with an empty ``TASK_CALL``, so spawn sites used to fall back to a fresh uuid, and
nothing linked the task to the model's tool call. After a crash, the call had no
observation and recovery could not say what it had started, so the agent started it
again (``docs/design/stop-and-restart.md``).

The id now carries both:

* the tool observer's id of the call (``call_<hex>``), unique per call, so two calls
  never share a handle even when a provider reuses tool-call ids; and
* the model's own tool-call id (the ``id`` recorded on the call's ``tool_call``
  segment), so recovery can find what an unanswered call started.

They are joined by :data:`MODEL_CALL_SEPARATOR`. A task-capable call keeps its own
``TASK_CALL`` id, which is already durable.
"""

from __future__ import annotations

from collections.abc import Iterable
from typing import Any
from uuid import uuid4

#: Joins the observer id and the model call id inside an invocation id.
MODEL_CALL_SEPARATOR = "~"


def model_call_id() -> str:
    """The model's tool-call id for the call now running, or ``""``."""

    from clio_agent.gact.artifacts.observer_bridge import MODEL_CALL_SLOT  # noqa: PLC0415

    return str((MODEL_CALL_SLOT.get() or {}).get("model_call_id") or "")


def spawn_invocation_id() -> str:
    """The invocation id for work started by the tool call now running."""

    from clio_agent.gact.artifacts.observer_bridge import current_call_id  # noqa: PLC0415
    from clio_agent.tools.task_call_context import TASK_CALL  # noqa: PLC0415

    task_call = TASK_CALL.get().invocation_id
    if task_call:
        return task_call
    observed = current_call_id() or uuid4().hex
    model = model_call_id()
    return f"{observed}{MODEL_CALL_SEPARATOR}{model}" if model else observed


def started_by(invocation_id: str, call_id: str) -> bool:
    """Whether ``invocation_id`` was minted by the model tool call ``call_id``.

    Spawn sites add prefixes (``flow:``) and suffixes (``:<index>`` for a batch), so the
    model call id is matched as the token after the separator, up to the next ``:``.
    """

    if not call_id or MODEL_CALL_SEPARATOR not in invocation_id:
        return False
    tail = invocation_id.rsplit(MODEL_CALL_SEPARATOR, 1)[1]
    return tail == call_id or tail.startswith(call_id + ":")


def started_tasks(history: Any, session_id: str, call_id: str) -> list[Any]:
    """The tasks the model call ``call_id`` in ``session_id`` started, oldest first.

    Only the conversation's own work and the children of its own assignments: a
    grandchild's invocation belongs to another conversation's model calls.
    """

    if not call_id:
        return []
    own = history.session_handles(session_id)
    rows: Iterable[Any] = (
        history.task_snapshot(handle) for handle in history.tree_handles(session_id)
    )
    found = [
        row
        for row in rows
        if row is not None
        and (row.owner.session_id == session_id or row.owner.parent_handle in own)
        and started_by(row.invocation_id, call_id)
    ]
    return sorted(found, key=lambda row: row.created_at)


__all__ = [
    "MODEL_CALL_SEPARATOR",
    "model_call_id",
    "spawn_invocation_id",
    "started_by",
    "started_tasks",
]
