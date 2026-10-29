"""Deliver observed terminal tool state even when durable recording fails."""

from __future__ import annotations

import logging
from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any

from clio_agent.gact.events import Event

logger = logging.getLogger(__name__)


@contextmanager
def terminal_tool_delivery(app: Any, sid: str, payload: dict[str, Any]) -> Iterator[None]:
    """Publish the actual execution result once, then propagate recording failures.

    This event is an observation of a tool that already returned. It must not be
    mistaken for proof of durable storage, nor leave a running command behind
    when recording its result fails. No execution is retried here.
    """
    delivered = payload
    try:
        yield
    except BaseException as exc:
        delivered = {
            **payload,
            "recording_status": "failed",
            "recording_error": str(exc),
        }
        logger.exception(
            "reason=tool_completion_recording_failed session=%s call=%s",
            sid,
            payload.get("call_id"),
        )
        raise
    finally:
        from clio_agent.gact.tool_execution_projection import completion_execution

        try:
            execution = completion_execution(app, sid, delivered)
        except Exception:
            logger.exception(
                "reason=tool_execution_projection_unavailable session=%s call=%s",
                sid,
                payload.get("call_id"),
            )
            execution = {
                "id": str(payload.get("call_id") or ""),
                "session_id": sid,
                "name": str(payload.get("tool") or "Tool"),
                "state": "unknown",
                "output": payload.get("result"),
                "execution_problem": "execution_projection_unavailable",
                "error": "The execution task could not be verified. Inspect the submission response before retrying.",
            }
        if execution is not None:
            delivered = {**delivered, "execution": execution}
        app.state.bus.publish(Event(type="tool.call.completed", session_id=sid, payload=delivered))
        if execution is not None and (task := execution.get("execution_task")):
            # Re-read after the receipt event as well: task settlement can race its delivery.
            app.state.bus.publish(
                Event(
                    type="task.updated",
                    session_id=sid,
                    payload={
                        "handle": task["handle"],
                        "kind": "execution",
                        "revision": app.state.task_history.revision,
                        "owner_session_id": sid,
                        "invocation_id": str(payload.get("call_id") or ""),
                    },
                )
            )
