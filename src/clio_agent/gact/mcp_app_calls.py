"""Saved opening calls and reopening for MCP Apps (plan MA18 and MA19).

When a tool result opens a view, the call that opened it is saved with the
session's own record in clio-core: the session metadata key ``mcp_app_calls``,
keyed by app instance. A saved call is ``{server, tool, arguments,
display_mode, resource_uri, label}`` plus bookkeeping (``saved_at``, and
``reopened_from`` for a call saved by a reopen). The private tool result and
the view's HTML are never saved: they stay on the in-memory registry record.

Reopening replays the saved call through the same gated executor path a view's
own ``tools/call`` uses (:func:`clio_agent.gact.mcp_apps._run_bound`), so the
session's tool permission rules apply and the server is reached exactly as for
any call. The replayed result is admitted by the ordinary MCP App observer; a
replay asks the observer, through :data:`_REPLAY`, to hand the new record back
instead of appending a second card to the transcript, because the card that
asked for the reopen mounts the result itself.

Every failed reopen returns a typed reason with the saved call, so the card
can show both and offer Retry. No reason is silent: each also reaches the
stream-audit sink and the log.
"""

from __future__ import annotations

import json
import logging
import secrets
import threading
from collections.abc import Mapping
from contextvars import ContextVar
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import TYPE_CHECKING, Any

from fastapi import FastAPI, HTTPException, Request

from clio_agent.gact.routes._body import json_body
from clio_agent.gact.types import ErrorEnvelope, ErrorInfo, Part
from clio_agent.runtime.stream_audit import stream_audit

if TYPE_CHECKING:
    from clio_agent.gact.mcp_apps import MCPAppRecord

logger = logging.getLogger(__name__)

SESSION_KEY = "mcp_app_calls"
DISPLAY_MODES = frozenset({"inline", "fullscreen"})
#: Arguments above this size are not kept in session metadata, which is sent to
#: the client in full. The entry is still saved, with a typed reason, so the
#: card can say why it cannot reopen.
MAX_SAVED_ARGUMENT_BYTES = 64 * 1024

#: Typed reasons a reopen can fail with. The UI shows ``detail``; the reason is
#: the stable key.
REOPEN_REASONS: dict[str, str] = {
    "no_saved_call": "This view's opening call was not saved, so it cannot be reopened.",
    "arguments_not_saved": "The opening call's arguments were too large to save.",
    "tool_unavailable": "The service that opened this view is not connected, or no longer "
    "offers the tool.",
    "tool_has_no_view": "The saved tool no longer opens a view.",
    "permission_denied": "The call was not allowed by this conversation's permission rules.",
    "call_failed": "The call could not be completed.",
    "tool_error": "The service returned an error.",
    "view_not_opened": "The call succeeded but its result did not open a view.",
}

_LOCK = threading.RLock()


@dataclass
class _ReplayCapture:
    """What a replay hands the observer, and what the observer hands back."""

    reopened_from: str
    display_mode: str
    record: MCPAppRecord | None = None


_REPLAY: ContextVar[_ReplayCapture | None] = ContextVar("mcp_app_replay", default=None)


class ReopenFailure(Exception):
    """A reopen that cannot proceed, with its typed reason."""

    def __init__(self, reason: str, detail: str = "") -> None:
        super().__init__(detail or REOPEN_REASONS[reason])
        self.reason = reason
        self.detail = detail or REOPEN_REASONS[reason]


# ---- the saved call -------------------------------------------------------


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _bare_tool(record: MCPAppRecord) -> str:
    namespace = record.source_namespace or ""
    prefix = f"{namespace}_"
    if namespace and record.tool_name.startswith(prefix):
        return record.tool_name[len(prefix) :]
    return record.tool_name


def _tool_title(tool: Any) -> str:
    """The tool's own title, from the definition or its annotations."""

    for source in (tool, getattr(tool, "annotations", None)):
        title = getattr(source, "title", None)
        if isinstance(title, str) and title.strip():
            return title.strip()
    if isinstance(tool, Mapping):
        title = tool.get("title") or (tool.get("annotations") or {}).get("title")
        if isinstance(title, str) and title.strip():
            return title.strip()
    return ""


def _label(tool: Any, bare: str) -> str:
    title = _tool_title(tool)
    if title:
        return title
    words = bare.replace("_", " ").replace("-", " ").strip()
    return words[:1].upper() + words[1:] if words else "Interactive view"


def saved_calls(app: FastAPI, sid: str) -> dict[str, dict[str, Any]]:
    """Every saved opening call of one session, keyed by app instance."""

    session = app.state.sessions.get(sid)
    if session is None:
        return {}
    calls = (session.metadata or {}).get(SESSION_KEY)
    if not isinstance(calls, Mapping):
        return {}
    return {str(key): dict(value) for key, value in calls.items() if isinstance(value, Mapping)}


def saved_call(app: FastAPI, sid: str, app_instance_id: str) -> dict[str, Any] | None:
    """The saved opening call of one app instance, or ``None``."""

    return saved_calls(app, sid).get(app_instance_id)


def _write(app: FastAPI, sid: str, app_instance_id: str, entry: dict[str, Any]) -> None:
    """Read, change and write the session's saved calls under one lock."""

    with _LOCK:
        calls = saved_calls(app, sid)
        calls[app_instance_id] = entry
        app.state.sessions.update(sid, metadata_patch={SESSION_KEY: calls})


def _saved_arguments(arguments: Mapping[str, Any]) -> tuple[dict[str, Any] | None, str]:
    encoded = json.dumps(dict(arguments), sort_keys=True, default=str).encode("utf-8")
    if len(encoded) > MAX_SAVED_ARGUMENT_BYTES:
        return None, "arguments_not_saved"
    # Round-trip so the stored value is plain JSON whatever the call carried.
    return json.loads(encoded), ""


def record_opening_call(app: FastAPI, sid: str, record: MCPAppRecord, tool: Any) -> bool:
    """Save the call that opened ``record`` (MA18).

    Called by the MCP App observer for every admitted view. Returns ``True``
    when the call is a person's reopen in progress: the replay then takes the
    record and the observer must not append another card.
    """

    capture = _REPLAY.get()
    bare = _bare_tool(record)
    arguments, unsaved = _saved_arguments(record.tool_input)
    entry: dict[str, Any] = {
        "server": record.source_namespace or "",
        "tool": bare,
        "arguments": arguments,
        "display_mode": capture.display_mode if capture else "inline",
        "resource_uri": record.resource_uri,
        "label": _label(tool, bare),
        "saved_at": _now(),
    }
    if unsaved:
        entry["unsaved_reason"] = unsaved
        _audit("mcp_app_call_not_saved", sid, record.app_instance_id, unsaved, entry)
    if capture is not None:
        entry["reopened_from"] = capture.reopened_from
    _write(app, sid, record.app_instance_id, entry)
    if capture is None:
        return False
    capture.record = record
    return True


def set_saved_display_mode(app: FastAPI, sid: str, app_instance_id: str, mode: str) -> bool:
    """Record a view's display mode on its saved call. Returns whether one was saved."""

    if mode not in DISPLAY_MODES:
        raise ValueError(f"unknown display mode {mode!r}")
    with _LOCK:
        entry = saved_call(app, sid, app_instance_id)
        if entry is None:
            return False
        if entry.get("display_mode") != mode:
            entry["display_mode"] = mode
            _write(app, sid, app_instance_id, entry)
        return True


# ---- reopening ------------------------------------------------------------


def _audit(
    stage: str, sid: str, app_instance_id: str, reason: str, entry: Mapping[str, Any] | None
) -> None:
    fields = {
        "session_id": sid,
        "app_instance_id": app_instance_id,
        "reason": reason,
        "server": (entry or {}).get("server", ""),
        "tool": (entry or {}).get("tool", ""),
    }
    stream_audit(stage, **fields)
    logger.warning("%s reason=%s app_instance_id=%s", stage, reason, app_instance_id)


def _resolve_opening_tool(executor: Any, entry: Mapping[str, Any]) -> str:
    """The executor's name for the saved tool, which must still open a view."""

    from clio_agent.gact.mcp_apps import _resource_uri  # noqa: PLC0415 - cycle

    server = str(entry.get("server") or "")
    tool = str(entry.get("tool") or "")
    if not server or not tool:
        raise ReopenFailure("no_saved_call")
    full_name = f"{server}_{tool}"
    definition = executor.get_all_tool_definitions().get(full_name)
    if definition is None:
        raise ReopenFailure(
            "tool_unavailable",
            f"The {server} service is not connected, or no longer offers {tool}.",
        )
    if not _resource_uri(definition):
        raise ReopenFailure("tool_has_no_view")
    return full_name


def _error_text(result_wire: Mapping[str, Any]) -> str:
    texts = [
        str(block.get("text") or "")
        for block in result_wire.get("content") or []
        if isinstance(block, Mapping) and block.get("type") == "text"
    ]
    return "\n".join(text for text in texts if text).strip()[:2000]


def _part(record: MCPAppRecord, display_mode: str) -> dict[str, Any]:
    from clio_agent.gact.mcp_apps import MCP_APP_MIME_TYPE  # noqa: PLC0415 - cycle
    from clio_agent.tools.mcp_extension_registry import (  # noqa: PLC0415
        MCP_APPS_PROTOCOL_REVISION,
    )

    return Part(
        id=f"mcp_app_{record.app_instance_id}",
        type="mcp_app",
        app_instance_id=record.app_instance_id,
        resource_uri=record.resource_uri,
        source_server=record.source_namespace or "",
        tool_name=record.tool_name,
        data_ref=record.data_ref,
        mime_type=MCP_APP_MIME_TYPE,
        metadata={
            "stream_source": "reopen",
            "protocol": MCP_APPS_PROTOCOL_REVISION,
            "display_mode": display_mode,
        },
    ).to_wire()


async def reopen_saved_call(app: FastAPI, sid: str, app_instance_id: str) -> dict[str, Any]:
    """Replay one saved opening call (MA19).

    Returns ``{part, saved_call}`` with a fresh ``mcp_app`` part, or
    ``{error: {reason, detail}, saved_call}``.
    """

    from clio_agent.gact.mcp_apps import (  # noqa: PLC0415 - cycle
        _run_bound,
        call_tool_result_to_wire,
    )

    entry = saved_call(app, sid, app_instance_id)

    def failed(reason: str, detail: str = "") -> dict[str, Any]:
        _audit("mcp_app_reopen_failed", sid, app_instance_id, reason, entry)
        return {
            "error": {"reason": reason, "detail": detail or REOPEN_REASONS[reason]},
            "saved_call": entry,
        }

    if entry is None:
        return failed("no_saved_call")
    if entry.get("unsaved_reason") or not isinstance(entry.get("arguments"), Mapping):
        return failed("arguments_not_saved")
    capture = _ReplayCapture(
        reopened_from=app_instance_id,
        display_mode=str(entry.get("display_mode") or "inline"),
    )
    arguments = dict(entry["arguments"])

    def replay(executor: Any) -> Any:
        full_name = _resolve_opening_tool(executor, entry)
        token = _REPLAY.set(capture)
        try:
            return executor.call_tool_result(full_name, arguments)
        finally:
            _REPLAY.reset(token)

    try:
        result = await _run_bound(app, sid, replay)
    except ReopenFailure as exc:
        return failed(exc.reason, exc.detail)
    except PermissionError as exc:
        return failed("permission_denied", str(exc) or REOPEN_REASONS["permission_denied"])
    except Exception as exc:  # noqa: BLE001 - forwards: the typed reason the card shows
        return failed("call_failed", f"{type(exc).__name__}: {exc}")
    wire = call_tool_result_to_wire(result)
    if wire.get("isError") is True:
        return failed("tool_error", _error_text(wire) or REOPEN_REASONS["tool_error"])
    if capture.record is None:
        return failed("view_not_opened")
    stream_audit(
        "mcp_app_reopened",
        session_id=sid,
        app_instance_id=capture.record.app_instance_id,
        reopened_from=app_instance_id,
        server=entry.get("server", ""),
        tool=entry.get("tool", ""),
    )
    return {
        "part": _part(capture.record, capture.display_mode),
        "saved_call": saved_call(app, sid, capture.record.app_instance_id),
    }


def _session_not_found() -> HTTPException:
    return HTTPException(
        status_code=404,
        detail=ErrorEnvelope(
            error=ErrorInfo(error="not_found", message="session not found", recoverable=False)
        ).model_dump(exclude_none=True),
    )


def app_is_live(app: FastAPI, sid: str, app_instance_id: str, data_ref: str) -> bool:
    """Whether CLIO still holds this view, without touching its idle time."""

    from clio_agent.gact.mcp_apps import _registry  # noqa: PLC0415 - cycle

    return any(
        record.app_instance_id == app_instance_id
        and secrets.compare_digest(record.data_ref, data_ref)
        for record in _registry(app).records_for_session(sid)
    )


def register_mcp_app_reopen_routes(app: FastAPI) -> None:
    """Register the reopen route and the card's liveness check."""

    @app.get("/v1/sessions/{sid}/mcp-apps/{app_id}/live")
    async def mcp_app_live(sid: str, app_id: str, data_ref: str) -> dict[str, Any]:
        """``{live}``: a card asks before mounting, so a closed view is not a failed request."""

        if app.state.sessions.get(sid) is None:
            raise _session_not_found()
        return {"live": app_is_live(app, sid, app_id, data_ref)}

    @app.post("/v1/sessions/{sid}/mcp-apps/reopen")
    async def reopen_mcp_app(sid: str, request: Request) -> dict[str, Any]:
        if app.state.sessions.get(sid) is None:
            raise _session_not_found()
        body = await json_body(request, route="POST MCP App reopen")
        app_instance_id = str(body.get("app_instance_id") or "").strip()
        if not app_instance_id:
            raise HTTPException(status_code=422, detail="app_instance_id is required")
        return await reopen_saved_call(app, sid, app_instance_id)


__all__ = [
    "DISPLAY_MODES",
    "MAX_SAVED_ARGUMENT_BYTES",
    "REOPEN_REASONS",
    "SESSION_KEY",
    "ReopenFailure",
    "app_is_live",
    "record_opening_call",
    "register_mcp_app_reopen_routes",
    "reopen_saved_call",
    "saved_call",
    "saved_calls",
    "set_saved_display_mode",
]
