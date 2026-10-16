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
        app.state.bus.publish(Event(type="tool.call.completed", session_id=sid, payload=delivered))
