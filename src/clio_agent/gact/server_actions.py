"""Actions on a server-owned A2UI surface go to that server (host semantics HS2).

A surface an MCP server created (HS1, or a plugin tab in HS3) sends its actions back to
the same server as a ``tools/call``: the action ``name`` is the server's tool name and
its ``context`` is the call's arguments, verbatim. The tool must be visible to apps
(``_meta.ui.visibility`` absent or including ``"app"``). ``data_source/login/<provider>``
is CLIO's own sign-in action instead (HS4). Nothing here names a particular server.
"""

from __future__ import annotations

import logging
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from typing import Any

from clio_agent.gact.server_surfaces import PayloadOutcome, ServerScope
from clio_agent.plugins.manifest import ACCOUNT_PROVIDERS

logger = logging.getLogger(__name__)

LOGIN_ACTION_PREFIX = "data_source/login/"


class ServerActionError(RuntimeError):
    """A typed refusal of a server action (HTTP ``status`` with ``reason``)."""

    def __init__(self, status: int, reason: str, message: str) -> None:
        self.status = status
        self.reason = reason
        super().__init__(message)


@dataclass
class ServerActionOutcome:
    """What one delivered server action did."""

    tool: str
    outcome: str
    surface_ids: list[str] = field(default_factory=list)
    error_text: str = ""
    task_handle: str = ""

    def wire(self, action_id: str) -> dict[str, Any]:
        row: dict[str, Any] = {
            "status": "delivered",
            "destination": "server",
            "action_id": action_id,
            "tool": self.tool,
            "outcome": self.outcome,
            "surface_ids": list(self.surface_ids),
        }
        if self.error_text:
            row["error_text"] = self.error_text
        if self.task_handle:
            row["task_handle"] = self.task_handle
        return row


def login_provider(name: str) -> str:
    """The provider of a ``data_source/login/<provider>`` action, else ""."""

    if not name.startswith(LOGIN_ACTION_PREFIX):
        return ""
    provider = name[len(LOGIN_ACTION_PREFIX) :]
    return provider if provider in ACCOUNT_PROVIDERS else ""


def login_context(app: Any, session_id: str) -> dict[str, str]:
    """The context CLIO fills for its own sign-in action (the server sends none)."""

    service = getattr(app.state, "connected_storage", None)
    session = app.state.sessions.get(session_id)
    if service is None or session is None:
        raise ServerActionError(
            503, "a2ui_source_login_out_of_scope", "account sign-in is not available here"
        )
    return {"clio_id": str(service.store.clio_id), "workspace_id": str(session.workspace_id)}


def resolve_server_tool(executor: Any, namespace: str, name: str) -> str:
    """``<namespace>_<name>`` when it exists and apps may call it.

    Raises:
        ServerActionError: 403 ``a2ui_server_tool_not_app_visible`` otherwise.
    """

    from clio_agent.gact.mcp_apps import _tool_visible_to_app  # noqa: PLC0415

    full = f"{namespace}_{name}"
    tool = executor.get_all_tool_definitions().get(full) if name else None
    if tool is None or not _tool_visible_to_app(tool):
        raise ServerActionError(
            403,
            "a2ui_server_tool_not_app_visible",
            f"tool {name!r} is not callable from a surface of server {namespace!r}",
        )
    return full


def task_handle(raw_result: Any) -> str:
    """The CLIO task handle when ``raw_result`` is a task acceptance receipt, else ""."""

    from clio_agent.tools.task_receipt import AcceptedTaskResult  # noqa: PLC0415

    structured = getattr(raw_result, "structured_content", None)
    if structured is None and isinstance(raw_result, Mapping):
        structured = raw_result.get("structuredContent")
    if not isinstance(structured, Mapping):
        return ""
    handle = str(structured.get("handle") or "")
    if not handle.startswith("task_"):
        return ""
    if isinstance(raw_result, AcceptedTaskResult) or "accepted" in structured:
        return handle if structured.get("accepted") is not False else ""
    return ""


def is_error_result(raw_result: Any) -> bool:
    value = getattr(raw_result, "is_error", None)
    if value is None and isinstance(raw_result, Mapping):
        value = raw_result.get("isError")
    return bool(value)


def error_text(raw_result: Any) -> str:
    from clio_agent.tools.mcp_result_text import result_to_text  # noqa: PLC0415

    try:
        return result_to_text(raw_result)
    except (TypeError, ValueError, AttributeError, KeyError):  # an unreadable error result
        return "the tool reported an error"


async def call_server_action(
    app: Any, session_id: str, scope: ServerScope, name: str, context: Mapping[str, Any]
) -> Any:
    """Send ``tools/call`` for one action, as a view call outside any turn."""

    from clio_agent.gact.mcp_apps import _run_bound  # noqa: PLC0415
    from clio_agent.tools import view_only_results  # noqa: PLC0415

    def call(executor: Any) -> Any:
        full = resolve_server_tool(executor, scope.namespace, name)
        definition = executor.get_all_tool_definitions().get(full)
        with view_only_results.scope(full, definition):
            return executor.call_tool_result(full, dict(context))

    try:
        return await _run_bound(app, session_id, call)
    except ServerActionError:
        raise
    except ValueError as exc:
        if "unknown MCP namespace" in str(exc):
            raise ServerActionError(503, "a2ui_server_unavailable", str(exc)) from exc
        raise


async def deliver_server_action(
    app: Any,
    session_id: str,
    scope: ServerScope,
    name: str,
    context: Mapping[str, Any],
    *,
    apply: Callable[[Any], list[PayloadOutcome] | None],
) -> ServerActionOutcome:
    """Call the server for one action and apply the A2UI its result carries.

    ``apply`` applies the result's A2UI where the surface lives (the conversation or a
    tab) and runs off the event loop.

    Raises:
        ServerActionError: 403, 503, or 422 with the HS1 reason when every A2UI payload
            in the result was refused.
    """

    from clio_agent.gact.off_loop import run_off_loop  # noqa: PLC0415

    raw = await call_server_action(app, session_id, scope, name, context)
    handle = task_handle(raw)
    if handle:
        return ServerActionOutcome(tool=name, outcome="task_pending", task_handle=handle)
    if is_error_result(raw):
        return ServerActionOutcome(tool=name, outcome="tool_error", error_text=error_text(raw))
    outcomes = await run_off_loop(apply, raw)
    if not outcomes:
        return ServerActionOutcome(tool=name, outcome="no_update")
    applied = [outcome for outcome in outcomes if not outcome.reason]
    if not applied:
        refused = outcomes[0]
        raise ServerActionError(422, refused.reason, refused.detail or refused.reason)
    surface_ids = list(dict.fromkeys(sid for outcome in applied for sid in outcome.surface_ids))
    return ServerActionOutcome(tool=name, outcome="applied", surface_ids=surface_ids)


__all__ = [
    "LOGIN_ACTION_PREFIX",
    "ServerActionError",
    "ServerActionOutcome",
    "call_server_action",
    "deliver_server_action",
    "error_text",
    "is_error_result",
    "login_context",
    "login_provider",
    "resolve_server_tool",
    "task_handle",
]
