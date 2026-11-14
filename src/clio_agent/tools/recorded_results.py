"""What CLIO records of a tool result: whole when small, a bounded record and a file when large.

The model's copy of a tool result is already bounded where it is produced
(:func:`clio_agent.tools.mcp_result_projection.bounded_model_tool_result`): past its room
the full text goes to the session's tool-output folder. CLIO's own records were not.
The tool observer kept the whole result in the trace's ``tool.call.completed`` event and
in the transcript row (``structured_content``, ``content_blocks``), and the task ledger
kept it as the call's inline value. One NASA Earthdata granule search wrote a 33.9 MB
segment into a 128 MB clio-core store (GG-F3), and a view's chunked reads filled one
(MC-F3).

Every record CLIO writes of a result now goes through this module:

* a view's call to a tool the model cannot see keeps only a size and hash
  (:mod:`clio_agent.tools.view_only_results`);
* any other result larger than :func:`record_bytes` is written once to the session's
  tool-output folder (content-addressed, removed with the session) and recorded as its
  text head (:func:`~clio_agent.tools.mcp_result_projection.transcript_tool_result_chars`)
  plus ``stored_result``: the typed reason :data:`TOOL_RESULT_STORED_IN_FILE`, its size,
  SHA-256 and file. A file that cannot be written is recorded with the typed reason
  :data:`TOOL_RESULT_NOT_STORED` instead, never inline.

What the model and a view receive is unchanged.
"""

from __future__ import annotations

import dataclasses
import hashlib
import json
from collections.abc import Mapping
from pathlib import Path
from typing import Any

import mcp_types

from clio_agent import conf
from clio_agent.runtime import trace
from clio_agent.tools import view_only_results

#: Typed reason: the result's full record is a file, the record keeps its head.
TOOL_RESULT_STORED_IN_FILE = "tool_result_stored_in_file"
#: Typed reason: the file could not be written; only the head and the size are kept.
TOOL_RESULT_NOT_STORED = "tool_result_not_stored"


def record_bytes() -> int:
    """The largest tool result (JSON bytes) a record keeps inline.

    Config: ``limits.tool_result_record_bytes`` / ``CLIO_TOOL_RESULT_RECORD_BYTES``
    (default 524288, 512 KiB). Past it, the record is the result's head and a file.
    """

    return max(
        1024,
        conf.resolve(
            "limits.tool_result_record_bytes",
            env="CLIO_TOOL_RESULT_RECORD_BYTES",
            default=512 * 1024,
            cast=conf.as_int,
        ),
    )


def _body(value: Any) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), default=str).encode()


def _head(content: Any) -> str:
    from clio_agent.tools.mcp_result_projection import (  # noqa: PLC0415
        transcript_tool_result_chars,
    )

    texts = [
        str(block.get("text") or "")
        for block in content or ()
        if isinstance(block, Mapping) and block.get("type") == "text"
    ]
    return "\n".join(texts)[: transcript_tool_result_chars()]


def _store(body: bytes) -> dict[str, Any]:
    """Write ``body`` once to the session's tool-output folder; the record of where."""
    from clio_agent.tools.execution import get_active_tool_workspace_root  # noqa: PLC0415
    from clio_agent.tools.servers.shell_spill_store import (  # noqa: PLC0415
        active_session_id,
        spill_directory,
    )

    digest = hashlib.sha256(body).hexdigest()
    record: dict[str, Any] = {"bytes": len(body), "sha256": digest}
    try:
        root = get_active_tool_workspace_root() or str(Path.cwd())
        folder = spill_directory(root, session_id=active_session_id())
        folder.mkdir(parents=True, exist_ok=True)
        path = folder / f"result-{digest[:16]}.json"
        if not path.exists():
            tmp = path.with_suffix(".tmp")
            tmp.write_bytes(body)
            tmp.replace(path)
    except OSError as exc:
        trace.event("TOOLS", "%s bytes=%d error=%r", TOOL_RESULT_NOT_STORED, len(body), exc)
        return {"reason": TOOL_RESULT_NOT_STORED, **record, "error": str(exc)[:300]}
    trace.event("TOOLS", "%s bytes=%d file=%s", TOOL_RESULT_STORED_IN_FILE, len(body), path)
    return {"reason": TOOL_RESULT_STORED_IN_FILE, **record, "file": str(path)}


def _note(stored: Mapping[str, Any]) -> str:
    where = f" Full result: {stored['file']}" if "file" in stored else ""
    return f"[clio: {stored['reason']}] The result was {stored['bytes']:,} bytes.{where}"


def observed(result: Any) -> Any:
    """The tool observer's copy of a result (trace, transcript row)."""

    result = view_only_results.observed_result(result)
    if result is None:
        return None
    body = result.encode() if isinstance(result, str) else _body(result)
    if len(body) <= record_bytes():
        return result
    stored = _store(body)
    if not isinstance(result, Mapping):  # a native tool's text or value
        text = result if isinstance(result, str) else body.decode(errors="replace")
        return f"{_note(stored)}\n\n{_head([{'type': 'text', 'text': text}])}".rstrip()
    head = _head(result.get("content"))
    return {
        "content": [{"type": "text", "text": f"{_note(stored)}\n\n{head}".rstrip()}],
        "structuredContent": None,
        "isError": bool(result.get("isError")),
        "stored_result": stored,
    }


def ledger_reply(reply: Any) -> Any:
    """The task ledger's copy of an MCP reply (the call's inline value)."""

    reply = view_only_results.stored_reply(reply)
    if not hasattr(reply, "content"):
        return reply
    content = [
        block.model_dump(mode="json", by_alias=True, exclude_none=True)
        if hasattr(block, "model_dump")
        else block
        for block in reply.content or ()
    ]
    is_error = bool(getattr(reply, "is_error", False))
    wire = {
        "content": content,
        "structuredContent": getattr(reply, "structured_content", None),
        "isError": is_error,
    }
    body = _body(wire)
    if len(body) <= record_bytes():
        return reply
    stored = _store(body)
    update: dict[str, Any] = {
        "content": [
            mcp_types.TextContent(type="text", text=f"{_note(stored)}\n\n{_head(content)}".rstrip())
        ],
        "structured_content": {"stored_result": stored},
    }
    if dataclasses.is_dataclass(reply) and not isinstance(reply, type):
        if any(field.name == "data" for field in dataclasses.fields(reply)):
            update["data"] = None
        return dataclasses.replace(reply, **update)
    return reply.model_copy(update=update)


__all__ = [
    "TOOL_RESULT_NOT_STORED",
    "TOOL_RESULT_STORED_IN_FILE",
    "ledger_reply",
    "observed",
    "record_bytes",
]
