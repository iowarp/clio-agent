"""Which tool call a call was made from, for calls made inside another call.

A ``run_code`` script's tool calls are children of its call: it binds its own call id
in :data:`PARENT_CALL_ID` for the calls it dispatches, and the observer stamps
``parent_call_id`` on each child's ``tool.call.started`` payload and live ``tool_call``
part, so readers can nest the rows under the script's.
"""

from __future__ import annotations

from contextvars import ContextVar
from typing import Any

__all__ = ["PARENT_CALL_ID", "live_call_metadata", "nested_call_fields"]

PARENT_CALL_ID: ContextVar[str] = ContextVar("clio_parent_call_id", default="")


def nested_call_fields() -> dict[str, Any]:
    """``{"parent_call_id": ...}`` for a call made inside another, else ``{}``."""
    parent = PARENT_CALL_ID.get()
    return {"parent_call_id": parent} if parent else {}


def live_call_metadata(started_at: str, thought_step_id: str | None) -> dict[str, Any]:
    """The metadata of a live ``tool_call`` part when it starts."""
    return {
        "started_at": started_at,
        "stream_source": "live",
        "telemetry_source": "live_observer",
        **({"thought_step_id": thought_step_id} if thought_step_id else {}),
        **nested_call_fields(),
    }
