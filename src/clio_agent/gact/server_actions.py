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
    toast: dict[str, Any] | None = None

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
        if self.toast:
            row["toast"] = dict(self.toast)
        return row


#: The ``_meta`` key of a tool result's short notice for the person (see :func:`result_toast`).
TOAST_META_KEY = "io.iowarp/toast"
TOAST_TONES = frozenset({"neutral", "positive", "warning", "negative"})
_TOAST_MESSAGE_MAX = 300
_TOAST_UNDO_LABEL_MAX = 40


def _result_meta(raw_result: Any) -> Mapping[str, Any]:
    meta = getattr(raw_result, "meta", None)
    if meta is None and isinstance(raw_result, Mapping):
        meta = raw_result.get("_meta", raw_result.get("meta"))
    return meta if isinstance(meta, Mapping) else {}


def _undo(value: Any) -> dict[str, Any] | None:
    if not isinstance(value, Mapping):
        return None
    label, action = value.get("label"), value.get("action")
    if not isinstance(label, str) or not 1 <= len(label.strip()) <= _TOAST_UNDO_LABEL_MAX:
        return None
    event = action.get("event") if isinstance(action, Mapping) else None
    if not isinstance(event, Mapping):
        return None
    event_name, context = event.get("name"), event.get("context", {})
    if not isinstance(event_name, str) or not event_name.strip():
        return None
    if not isinstance(context, Mapping):
        return None
    return {"label": label.strip(), "action": {"event": dict(event)}}


def result_toast(raw_result: Any, *, tool: str = "") -> dict[str, Any] | None:
    """The valid ``_meta["io.iowarp/toast"]`` of a tool result, else ``None``.

    A toast is ``{message, tone, undo?: {label, action}}``: ``message`` 1..300
    characters, ``tone`` one of :data:`TOAST_TONES` (``neutral`` when absent), and
    ``undo`` a label of 1..40 characters with an A2UI event action
    (``{"event": {"name", "context"?}}``) the surface sends like any other. An invalid toast is dropped with a log line; it never
    fails the action.
    """

    value = _result_meta(raw_result).get(TOAST_META_KEY)
    if value is None:
        return None
    problem = ""
    toast: dict[str, Any] = {}
    if not isinstance(value, Mapping):
        problem = "not an object"
    else:
        message, tone = value.get("message"), value.get("tone", "neutral")
        if not isinstance(message, str) or not 1 <= len(message.strip()) <= _TOAST_MESSAGE_MAX:
            problem = "message must be 1 to 300 characters"
        elif tone not in TOAST_TONES:
            problem = f"tone {tone!r} is not one of {sorted(TOAST_TONES)}"
        else:
            toast = {"message": message.strip(), "tone": tone}
            if value.get("undo") is not None:
                undo = _undo(value["undo"])
                if undo is None:
                    problem = "undo needs a 1 to 40 character label and an A2UI event action"
                else:
                    toast["undo"] = undo
    if problem:
        logger.warning("a2ui_action_toast_dropped tool=%s reason=%s", tool or "?", problem)
        return None
    return toast


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


def server_tools(executor: Any, namespace: str) -> list[Any]:
    """The server's own tool definitions, as bare-named entries.

    Read from the server's current ``tools/list`` over the namespace's connection, not
    from the executor's turn catalog: that catalog is empty until the session's first
    turn, and a tab's action can come before any turn (after an agent restart, say).
    An executor that cannot list one namespace falls back to its catalog.
    """

    from clio_agent.tools.tool_ui_metadata import _tool_ui_metadata  # noqa: PLC0415

    lister = getattr(executor, "list_namespace_tools", None)
    if callable(lister):
        return list(lister(namespace))
    prefix = f"{namespace}_"
    return [
        {**dict(tool), "name": full[len(prefix) :]}
        if isinstance(tool, Mapping)
        else {"name": full[len(prefix) :], "_meta": {"ui": dict(_tool_ui_metadata(tool))}}
        for full, tool in executor.get_all_tool_definitions().items()
        if full.startswith(prefix)
    ]


def _listed_name(tool: Any) -> str:
    listed = tool.get("name") if isinstance(tool, Mapping) else getattr(tool, "name", "")
    return str(listed or "")


def resolve_server_tool_definition(
    executor: Any, namespace: str, name: str, *, listed: list[Any] | None = None
) -> tuple[str, Any]:
    """``(<namespace>_<name>, definition)`` when the server has the tool and apps may call it.

    Raises:
        ServerActionError: 403 ``a2ui_server_tool_not_app_visible`` otherwise.
    """

    from clio_agent.gact.mcp_apps import _tool_visible_to_app  # noqa: PLC0415

    tools = server_tools(executor, namespace) if listed is None and name else listed or []
    tool = next((tool for tool in tools if _listed_name(tool) == name), None) if name else None
    if tool is None or not _tool_visible_to_app(tool):
        raise ServerActionError(
            403,
            "a2ui_server_tool_not_app_visible",
            f"tool {name!r} is not callable from a surface of server {namespace!r}",
        )
    return f"{namespace}_{name}", tool


def resolve_server_tool(executor: Any, namespace: str, name: str) -> str:
    """``<namespace>_<name>`` when the server has the tool and apps may call it.

    Raises:
        ServerActionError: 403 ``a2ui_server_tool_not_app_visible`` otherwise.
    """

    return resolve_server_tool_definition(executor, namespace, name)[0]


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
    app: Any,
    session_id: str,
    scope: ServerScope,
    name: str,
    context: Mapping[str, Any],
    *,
    place: str = "",
    place_title: str = "",
) -> Any:
    """Send ``tools/call`` for one action, as a view call outside any turn.

    The call asks one consent for all the server's view-only tools, worded with the
    server's title and the ``place`` (page, tab or conversation) it was acted on in.
    """

    from clio_agent.gact import surface_consent  # noqa: PLC0415
    from clio_agent.gact.mcp_apps import _run_bound  # noqa: PLC0415
    from clio_agent.tools import view_only_results  # noqa: PLC0415

    def call(executor: Any) -> Any:
        listed = server_tools(executor, scope.namespace)
        full, definition = resolve_server_tool_definition(
            executor, scope.namespace, name, listed=listed
        )
        consent = surface_consent.for_surface(
            scope.namespace,
            str(scope.host.get("title") or scope.plugin or scope.namespace),
            listed,
            place=place or surface_consent.PLACE_CONVERSATION,
            place_title=place_title,
        )
        with view_only_results.scope(full, definition), surface_consent.scope(consent):
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
    place: str = "",
    place_title: str = "",
) -> ServerActionOutcome:
    """Call the server for one action and apply the A2UI its result carries.

    ``apply`` applies the result's A2UI where the surface lives (the conversation or a
    tab) and runs off the event loop. ``place`` and ``place_title`` word the consent.

    Raises:
        ServerActionError: 403, 503, or 422 with the HS1 reason when every A2UI payload
            in the result was refused.
    """

    from clio_agent.gact.off_loop import run_off_loop  # noqa: PLC0415

    raw = await call_server_action(
        app, session_id, scope, name, context, place=place, place_title=place_title
    )
    toast = result_toast(raw, tool=name)
    handle = task_handle(raw)
    if handle:
        return ServerActionOutcome(
            tool=name, outcome="task_pending", task_handle=handle, toast=toast
        )
    if is_error_result(raw):
        return ServerActionOutcome(
            tool=name, outcome="tool_error", error_text=error_text(raw), toast=toast
        )
    outcomes = await run_off_loop(apply, raw)
    if not outcomes:
        return ServerActionOutcome(tool=name, outcome="no_update", toast=toast)
    applied = [outcome for outcome in outcomes if not outcome.reason]
    if not applied:
        refused = outcomes[0]
        raise ServerActionError(422, refused.reason, refused.detail or refused.reason)
    surface_ids = list(dict.fromkeys(sid for outcome in applied for sid in outcome.surface_ids))
    return ServerActionOutcome(tool=name, outcome="applied", surface_ids=surface_ids, toast=toast)


__all__ = [
    "LOGIN_ACTION_PREFIX",
    "TOAST_META_KEY",
    "TOAST_TONES",
    "ServerActionError",
    "ServerActionOutcome",
    "call_server_action",
    "deliver_server_action",
    "error_text",
    "is_error_result",
    "login_context",
    "login_provider",
    "resolve_server_tool",
    "resolve_server_tool_definition",
    "result_toast",
    "server_tools",
    "task_handle",
]
