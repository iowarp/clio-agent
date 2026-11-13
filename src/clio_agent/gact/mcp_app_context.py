"""What a live MCP App view reports reaches the agent (MA8 to MA10; owner module).

A view reports context with ``ui/update-model-context``; the surface posts it to
``PUT /v1/sessions/{sid}/mcp-apps/{app_id}/model-context``. The last report of each
app instance is stored with the session's context in clio-core: the session record's
metadata under :data:`APP_CONTEXT_KEY`, keyed by ``app_instance_id`` (no fifth store;
the in-memory registry record keeps no copy).

Two paths put it in model text, through one function (:func:`app_context_block`):

* a message the person types: the turn-start working state carries one block per live
  app that reported context or offers its own tools (:func:`app_context_injection`,
  source :data:`APP_CONTEXT_SOURCE`). As working state it is recorded only when it
  changed and is offered again after a compaction;
* a message the view sends itself (``ui/message``): its block follows the message text.

"Live" is the registry's admitted records for the session (an app the person closed
or one that expired is not live).
"""

from __future__ import annotations

import hashlib
import json
import threading
import time
from collections.abc import Mapping
from typing import Any

__all__ = [
    "APP_CONTEXT_KEY",
    "APP_CONTEXT_SOURCE",
    "MAX_MODEL_CONTEXT_BYTES",
    "AppContextTooLarge",
    "app_context_block",
    "app_context_injection",
    "forget_app_context",
    "forget_closed_app",
    "provenance",
    "stored_app_context",
    "store_app_context",
]

#: Session metadata key (one clio-core session record) holding each app's last report.
APP_CONTEXT_KEY = "mcp_app_context"
#: The injection source and core reminder (``clio.reminder.mcp_app_context``).
APP_CONTEXT_SOURCE = "mcp_app_context"
#: One app's context, as encoded JSON (MA10: the existing 128 KiB cap).
MAX_MODEL_CONTEXT_BYTES = 128 * 1024

_LOCK = threading.Lock()


class AppContextTooLarge(ValueError):
    """A report over :data:`MAX_MODEL_CONTEXT_BYTES` (the route answers 413)."""


def _entries(app: Any, sid: str) -> dict[str, dict[str, Any]]:
    session = app.state.sessions.get(sid)
    metadata = getattr(session, "metadata", None)
    rows = metadata.get(APP_CONTEXT_KEY) if isinstance(metadata, Mapping) else None
    return {str(k): dict(v) for k, v in rows.items()} if isinstance(rows, Mapping) else {}


def store_app_context(app: Any, record: Any, body: Mapping[str, Any]) -> dict[str, Any]:
    """Store the view's latest report for ``record``; return its provenance.

    Raises:
        AppContextTooLarge: the encoded report is over the cap; nothing is stored.
    """
    encoded = json.dumps(dict(body), sort_keys=True, default=str).encode("utf-8")
    if len(encoded) > MAX_MODEL_CONTEXT_BYTES:
        raise AppContextTooLarge("MCP App model context is too large")
    digest = hashlib.sha256(encoded).hexdigest()
    entry = {
        "server": record.source_namespace or "",
        "tool": record.tool_name,
        "resource_uri": record.resource_uri,
        "context": json.loads(encoded),
        "sha256": digest,
        "bytes": len(encoded),
        "reported_at": time.time(),
    }
    with _LOCK:
        rows = _entries(app, record.session_id)
        previous = rows.get(record.app_instance_id) or {}
        if previous.get("sha256") == digest:
            return provenance(previous)
        rows[record.app_instance_id] = entry
        app.state.sessions.update(record.session_id, metadata_patch={APP_CONTEXT_KEY: rows})
    return provenance(entry)


def stored_app_context(app: Any, sid: str, app_instance_id: str) -> dict[str, Any] | None:
    """The stored report of one app instance (read from clio-core), or ``None``."""
    return _entries(app, sid).get(app_instance_id)


def forget_app_context(app: Any, sid: str, app_instance_id: str) -> None:
    """Drop a closed app's report (it is no longer what the person sees)."""
    with _LOCK:
        rows = _entries(app, sid)
        if rows.pop(app_instance_id, None) is not None:
            app.state.sessions.update(sid, metadata_patch={APP_CONTEXT_KEY: rows})


def forget_closed_app(app: Any, sid: str, app_instance_id: str) -> None:
    """A closed app leaves the agent's view: its report and its tools go."""
    from clio_agent.gact.mcp_app_tools import app_tool_table  # noqa: PLC0415

    app_tool_table(app).drop_app(sid, app_instance_id)
    if app.state.sessions.get(sid) is not None:
        forget_app_context(app, sid, app_instance_id)


def provenance(entry: Mapping[str, Any] | None) -> dict[str, Any]:
    """What a message records about the context it carried (never the context)."""
    entry = entry or {}
    return {
        "present": bool(entry.get("context")),
        "sha256": str(entry.get("sha256") or ""),
        "bytes": int(entry.get("bytes") or 0),
    }


def app_context_block(
    app: Any,
    sid: str,
    *,
    server: str,
    tool: str,
    context: Any = None,
    tools: list[str] | None = None,
) -> str:
    """The one block a live app adds to model text (``clio.reminder.mcp_app_context``)."""
    from clio_agent import injections  # noqa: PLC0415

    text = injections.render(
        APP_CONTEXT_SOURCE,
        {
            "server": server or "server",
            "tool": tool or "a tool",
            "context": json.dumps(context, sort_keys=True, default=str) if context else "",
            "tools": ", ".join(tools or ()),
        },
        app=app,
        session_id=sid,
    )
    return text or ""


def app_context_injection(app: Any, sid: str) -> str:
    """The turn's block for every live app that reported context or offers tools."""
    from clio_agent.gact.mcp_app_tools import offered_names_by_app  # noqa: PLC0415
    from clio_agent.gact.mcp_apps import _registry  # noqa: PLC0415

    records = _registry(app).records_for_session(sid)
    if not records:
        return ""
    rows = _entries(app, sid)
    tools = offered_names_by_app(app, sid)
    blocks = []
    for record in sorted(records, key=lambda r: r.created_at):
        entry = rows.get(record.app_instance_id) or {}
        names = tools.get(record.app_instance_id, [])
        if not entry.get("context") and not names:
            continue
        block = app_context_block(
            app,
            sid,
            server=record.source_namespace or "",
            tool=record.tool_name,
            context=entry.get("context"),
            tools=names,
        )
        if block:
            blocks.append(block)
    return "\n\n".join(blocks)
