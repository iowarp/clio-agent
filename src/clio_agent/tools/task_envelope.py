"""Read a tool's own reply out of a settled native task envelope.

Kept free of SDK imports: the presentation layer reads it on the startup path, and
``task_receipt`` (acceptance and terminal validation) loads the task client.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any


def settled_tool_result(result: Any) -> Mapping[str, Any] | None:
    """The tool's own reply inside a settled native task envelope, or ``None``.

    A foreground native call (the shell) returns its task envelope rather than the
    tool's reply: ``{"handle": "task_…", "status", "pending": False, "result": <the
    recorded task result>}``, whose own ``result`` is the backend ``CallToolResult``
    (``content``/``structuredContent``/``isError``). The observer copy wraps that
    envelope as ``structuredContent``. A presentation reads the tool's reply, so it
    must look through the envelope; anything else is not a settled envelope.
    """
    envelope = result.get("structuredContent") if isinstance(result, Mapping) else None
    if (
        not isinstance(envelope, Mapping)
        or envelope.get("pending") is not False
        or not str(envelope.get("handle") or "").startswith("task_")
    ):
        return None
    record = envelope.get("result")
    reply = record.get("result") if isinstance(record, Mapping) else None
    return reply if isinstance(reply, Mapping) else None


__all__ = ["settled_tool_result"]
