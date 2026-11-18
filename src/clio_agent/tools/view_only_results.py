"""A view's own call to a tool the model never sees keeps a record, not its content (MC-F3).

An MCP App view calls tools of its own server (``tools/call``). A tool whose
``_meta.ui.visibility`` leaves out ``"model"`` exists only for the view: the PDF viewer
reads its file in 512 KB base64 parts on every mount, polls for commands every 30 s and
sends page images. The model never sees those calls, yet every one was stored in full:
in the trace's tool events, in the transcript, and in the task ledger's submission and
inline value. One conversation held 36.9 MB of them and filled a 128 MB clio-core store.

Inside :func:`scope` such a call is unchanged for the view (its reply is returned as the
server sent it). Every record CLIO writes of it holds a compact record instead of the
content: the tool, the size and SHA-256 of what was not stored, and the outcome, under the
typed reason :data:`VIEW_ONLY_CONTENT_NOT_STORED`. Small arguments (a view's ids and page
numbers) are kept as sent. The call writes no transcript row
(:func:`clio_agent.gact.view_call_transcript.hold`). Calls the model can see, and every
call the agent makes, keep their normal path: the scope is entered only by the route that
serves a view's ``tools/call``.
"""

from __future__ import annotations

import dataclasses
import hashlib
import json
from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from contextvars import ContextVar
from typing import Any

from clio_agent.tools.tool_ui_metadata import _tool_visible_to_model

#: Typed reason on every compact record (trace, task ledger).
VIEW_ONLY_CONTENT_NOT_STORED = "view_only_content_not_stored"
#: Arguments at or under this many JSON bytes are recorded as sent.
ARGUMENTS_KEPT_BYTES = 2048
#: How much of a failed call's text is kept, so a failure stays diagnosable.
ERROR_TEXT_KEPT_CHARS = 500

_TOOL: ContextVar[str | None] = ContextVar("clio_view_only_tool", default=None)


@contextmanager
def scope(tool: str, definition: Any) -> Iterator[None]:
    """Run one view call; records of it are compact when the model cannot see ``tool``."""

    if _tool_visible_to_model(tool, definition):
        yield
        return
    token = _TOOL.set(tool)
    try:
        yield
    finally:
        _TOOL.reset(token)


def active() -> str | None:
    """The view-only tool being called in this context, or ``None``."""

    return _TOOL.get()


def _measure(value: Any) -> tuple[int, str]:
    body = json.dumps(value, sort_keys=True, separators=(",", ":"), default=str).encode()
    return len(body), hashlib.sha256(body).hexdigest()


def _record(tool: str, value: Any, *, is_error: bool) -> dict[str, Any]:
    size, digest = _measure(value)
    record: dict[str, Any] = {
        "reason": VIEW_ONLY_CONTENT_NOT_STORED,
        "tool": tool,
        "bytes": size,
        "sha256": digest,
        "outcome": "error" if is_error else "ok",
    }
    if is_error:
        texts = value.get("content") if isinstance(value, Mapping) else None
        text = " ".join(
            str(block.get("text") or "")
            for block in texts or ()
            if isinstance(block, Mapping) and block.get("type") == "text"
        )
        if text:
            record["error_text"] = text[:ERROR_TEXT_KEPT_CHARS]
    return record


def _summary(record: Mapping[str, Any]) -> str:
    return (
        f"A result only the view reads ({record['bytes']:,} bytes, "
        f"sha256 {str(record['sha256'])[:12]}) was not stored."
    )


def recorded_arguments(arguments: Mapping[str, Any]) -> Mapping[str, Any]:
    """``arguments`` as recorded: as sent, unless a view-only call sent a large payload."""

    tool = _TOOL.get()
    if tool is None:
        return arguments
    size, digest = _measure(dict(arguments))
    if size <= ARGUMENTS_KEPT_BYTES:
        return arguments
    return {
        "view_only_arguments": {
            "reason": VIEW_ONLY_CONTENT_NOT_STORED,
            "tool": tool,
            "bytes": size,
            "sha256": digest,
            "keys": sorted(str(key) for key in arguments),
        }
    }


def observed_result(result: Any) -> Any:
    """The tool observer's copy of a result: compact for a view-only call."""

    tool = _TOOL.get()
    if tool is None or result is None:
        return result
    is_error = bool(isinstance(result, Mapping) and result.get("isError"))
    record = _record(tool, result, is_error=is_error)
    return {
        "content": [{"type": "text", "text": _summary(record)}],
        "structuredContent": {"view_only_result": record},
        "isError": is_error,
    }


def stored_reply(reply: Any) -> Any:
    """The task ledger's copy of an MCP reply: compact for a view-only call.

    ``reply`` is the client's result (a dataclass or a model with ``content``,
    ``structured_content`` and ``is_error``); the copy keeps its type.
    """

    tool = _TOOL.get()
    if tool is None or not hasattr(reply, "content"):
        return reply
    is_error = bool(getattr(reply, "is_error", False))
    wire = {
        "content": [
            block.model_dump(mode="json", by_alias=True, exclude_none=True)
            if hasattr(block, "model_dump")
            else block
            for block in reply.content or ()
        ],
        "structuredContent": getattr(reply, "structured_content", None),
        "isError": is_error,
    }
    record = _record(tool, wire, is_error=is_error)
    import mcp_types  # noqa: PLC0415 - the MCP SDK loads on first use (#702)

    update: dict[str, Any] = {
        "content": [mcp_types.TextContent(type="text", text=_summary(record))],
        "structured_content": {"view_only_result": record},
    }
    if dataclasses.is_dataclass(reply) and not isinstance(reply, type):
        if any(field.name == "data" for field in dataclasses.fields(reply)):
            update["data"] = None
        return dataclasses.replace(reply, **update)
    return reply.model_copy(update=update)


__all__ = [
    "ARGUMENTS_KEPT_BYTES",
    "ERROR_TEXT_KEPT_CHARS",
    "VIEW_ONLY_CONTENT_NOT_STORED",
    "active",
    "observed_result",
    "recorded_arguments",
    "scope",
    "stored_reply",
]
