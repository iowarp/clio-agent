"""A view's own tools, offered to the agent and routed to the open view (MA11 to MA14).

Draft "App-Provided Tools" (ext-apps ``specification/draft`` at ``ea3e9d0``): a view that
declares ``appCapabilities.tools`` in ``ui/initialize`` answers ``tools/list`` and
``tools/call`` sent by the host, and sends ``notifications/tools/list_changed`` when its
list changes. The surface holding the view lists them and posts the list to
``PUT .../mcp-apps/{app_id}/app-tools``; an unloading frame posts an empty list.

Each offered tool is a deferred tool of the session named ``app__<server>__<tool>``
(:func:`agent_tool_name`): it joins the deferred catalog ``search_tools`` reads and the
``@<server>`` summons (owner ``<server>``), and leaves it when the list drops it.

A call publishes the session event ``mcp_app_tool_call``; the surface forwards it to the
view as ``tools/call`` and posts the view's answer to
``POST .../mcp-apps/{app_id}/app-tool-results/{call_id}``. The call waits for that answer
with no timeout (rule 6): cancelling the turn releases it. A call when no surface holds
the app returns at once, naming the tool that opens it.

The table is runtime state that mirrors what open views offer, like the app registry it
sits beside; nothing here is persisted.
"""

from __future__ import annotations

import inspect
import json
import logging
import re
import secrets
import threading
from collections.abc import Mapping, MutableMapping, Sequence
from dataclasses import dataclass, field
from typing import Any

from clio_agent.runtime import trace

__all__ = [
    "APP_TOOL_CALL_EVENT",
    "APP_TOOL_PREFIX",
    "MAX_APP_TOOLS",
    "MAX_APP_TOOL_SCHEMA_BYTES",
    "AppTool",
    "AppToolTable",
    "agent_tool_name",
    "app_tool_refresher",
    "app_tool_table",
    "attach_app_tools",
    "call_app_tool",
    "offered_names_by_app",
    "place_app_tools",
]

logger = logging.getLogger(__name__)

APP_TOOL_PREFIX = "app__"
#: The session event a surface answers by forwarding ``tools/call`` to its view.
APP_TOOL_CALL_EVENT = "mcp_app_tool_call"
#: MA14, Claude's published limits: a view over either offers no tools.
MAX_APP_TOOLS = 64
MAX_APP_TOOL_SCHEMA_BYTES = 64 * 1024
#: Typed reasons, traced and returned to the surface.
APP_TOOLS_OVER_LIMIT = "mcp_app_tools_over_limit"
APP_TOOL_SCHEMA_TOO_LARGE = "mcp_app_tool_schema_too_large"
APP_TOOL_NAME_INVALID = "mcp_app_tool_name_invalid"
APP_TOOL_NOT_OPEN = "mcp_app_tool_not_open"
#: A tool name every model provider accepts.
_TOOL_NAME = re.compile(r"^[A-Za-z0-9_-]{1,64}$")


class AppToolsRejected(ValueError):
    """The view's list breaks a limit; it offers no tools (``reason`` is typed)."""

    def __init__(self, reason: str, message: str) -> None:
        super().__init__(message)
        self.reason = reason


def agent_tool_name(server: str, tool: str) -> str:
    """``app__<server>__<tool>``: the name the agent calls a view's tool by."""
    return f"{APP_TOOL_PREFIX}{server}__{tool}"


@dataclass(frozen=True)
class AppTool:
    """One tool a live view offers, as the agent sees it."""

    agent_name: str
    server: str
    app_instance_id: str
    name: str
    description: str
    input_schema: Mapping[str, Any]
    opening_tool: str
    read_only: bool = False


@dataclass
class PendingCall:
    """One agent call waiting for the view's answer."""

    call_id: str
    session_id: str
    app_instance_id: str
    tool: AppTool
    arguments: dict[str, Any]
    done: threading.Event = field(default_factory=threading.Event)
    result: dict[str, Any] | None = None
    error: str | None = None

    def event_payload(self) -> dict[str, Any]:
        return {
            "session_id": self.session_id,
            "app_instance_id": self.app_instance_id,
            "call_id": self.call_id,
            "name": self.tool.name,
            "arguments": self.arguments,
        }


def _validated(server: str, record: Any, tools: Sequence[Any]) -> tuple[list[AppTool], list[dict]]:
    if len(tools) > MAX_APP_TOOLS:
        raise AppToolsRejected(
            APP_TOOLS_OVER_LIMIT,
            f"The view lists {len(tools)} tools; at most {MAX_APP_TOOLS} are offered.",
        )
    accepted: list[AppTool] = []
    skipped: list[dict[str, str]] = []
    for row in tools:
        if not isinstance(row, Mapping):
            continue
        name = str(row.get("name") or "").strip()
        schema = row.get("inputSchema")
        schema = dict(schema) if isinstance(schema, Mapping) else {"type": "object"}
        if len(json.dumps(schema, default=str).encode("utf-8")) > MAX_APP_TOOL_SCHEMA_BYTES:
            raise AppToolsRejected(
                APP_TOOL_SCHEMA_TOO_LARGE,
                f"The input schema of {name or 'a tool'} is over "
                f"{MAX_APP_TOOL_SCHEMA_BYTES // 1024} KiB.",
            )
        agent_name = agent_tool_name(server, name)
        if not name or not _TOOL_NAME.match(agent_name):
            skipped.append({"name": name, "reason": APP_TOOL_NAME_INVALID})
            continue
        annotations = row.get("annotations")
        read_only = isinstance(annotations, Mapping) and annotations.get("readOnlyHint") is True
        accepted.append(
            AppTool(
                agent_name=agent_name,
                server=server,
                app_instance_id=record.app_instance_id,
                name=name,
                description=str(row.get("description") or ""),
                input_schema=schema,
                opening_tool=str(record.tool_name or ""),
                read_only=read_only,
            )
        )
    return accepted, skipped


class AppToolTable:
    """Per session: the tools each live view offers, and the calls waiting on views."""

    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._apps: dict[str, dict[str, list[AppTool]]] = {}
        self._seen: dict[str, dict[str, AppTool]] = {}
        self._calls: dict[str, PendingCall] = {}
        self.revision = 0

    def set_tools(self, record: Any, tools: Sequence[Any]) -> tuple[list[str], list[dict]]:
        """Replace what one view offers. Returns the offered names and the skipped rows.

        Raises:
            AppToolsRejected: the list breaks a limit; the view now offers nothing.
        """
        server = str(record.source_namespace or "")
        sid, app_id = record.session_id, record.app_instance_id
        try:
            accepted, skipped = _validated(server, record, tools)
        except AppToolsRejected:
            self.set_offered(sid, app_id, [])
            raise
        self.set_offered(sid, app_id, accepted)
        return [tool.agent_name for tool in accepted], skipped

    def set_offered(self, sid: str, app_id: str, tools: list[AppTool]) -> None:
        with self._lock:
            apps = self._apps.setdefault(sid, {})
            before = apps.pop(app_id, [])
            if tools:
                apps[app_id] = tools  # re-inserted last: the latest view wins a shared name
                seen = self._seen.setdefault(sid, {})
                for tool in tools:
                    seen[tool.agent_name] = tool
            if before or tools:
                self.revision += 1

    def offered(self, sid: str) -> dict[str, AppTool]:
        """The tools on offer now, by agent name (a later-registered view wins a name)."""
        with self._lock:
            out: dict[str, AppTool] = {}
            for tools in self._apps.get(sid, {}).values():
                out.update((tool.agent_name, tool) for tool in tools)
            return out

    def known(self, sid: str) -> dict[str, AppTool]:
        """Every app tool this session was offered, kept so a late call can be answered."""
        with self._lock:
            return dict(self._seen.get(sid, {}))

    def holds(self, sid: str, app_id: str) -> bool:
        with self._lock:
            return bool(self._apps.get(sid, {}).get(app_id))

    def begin(self, tool: AppTool, sid: str, arguments: Mapping[str, Any]) -> PendingCall:
        call = PendingCall(
            call_id=f"apcall_{secrets.token_urlsafe(12)}",
            session_id=sid,
            app_instance_id=tool.app_instance_id,
            tool=tool,
            arguments=dict(arguments),
        )
        with self._lock:
            self._calls[call.call_id] = call
        return call

    def pending_for_app(self, sid: str, app_id: str) -> list[PendingCall]:
        with self._lock:
            return [
                c
                for c in self._calls.values()
                if c.session_id == sid and c.app_instance_id == app_id and not c.done.is_set()
            ]

    def settle(
        self,
        sid: str,
        app_id: str,
        call_id: str,
        *,
        result: Mapping[str, Any] | None = None,
        error: str | None = None,
    ) -> bool:
        """Deliver the view's answer to the waiting call; ``False`` when none waits."""
        with self._lock:
            call = self._calls.get(call_id)
            if call is None or call.session_id != sid or call.app_instance_id != app_id:
                return False
            if call.done.is_set():
                return False
            self._calls.pop(call_id, None)
            call.result = dict(result) if result is not None else None
            call.error = error
            call.done.set()
            return True

    def forget(self, call: PendingCall) -> None:
        with self._lock:
            self._calls.pop(call.call_id, None)

    def drop_app(self, sid: str, app_id: str) -> None:
        """The app closed: it offers nothing and every waiting call learns it is not open."""
        self.set_offered(sid, app_id, [])
        for call in self.pending_for_app(sid, app_id):
            self.settle(sid, app_id, call.call_id, error=APP_TOOL_NOT_OPEN)

    def drop_session(self, sid: str) -> None:
        with self._lock:
            apps = list(self._apps.get(sid, {}))
        for app_id in apps:
            self.drop_app(sid, app_id)
        with self._lock:
            self._apps.pop(sid, None)
            self._seen.pop(sid, None)


def app_tool_table(app: Any) -> AppToolTable:
    """The app's table (created on first use)."""
    state = app.state
    table = getattr(state, "mcp_app_tools", None)
    if not isinstance(table, AppToolTable):
        table = AppToolTable()
        state.mcp_app_tools = table
    return table


def offered_names_by_app(app: Any, sid: str) -> dict[str, list[str]]:
    """The offered agent names, grouped by app instance."""
    out: dict[str, list[str]] = {}
    for tool in app_tool_table(app).offered(sid).values():
        out.setdefault(tool.app_instance_id, []).append(tool.agent_name)
    return out


def publish_call(app: Any, call: PendingCall) -> None:
    """Ask the surface holding the app to forward this call to its view."""
    from clio_agent.gact.events import Event  # noqa: PLC0415

    bus = getattr(app.state, "bus", None)
    if bus is not None:
        # Transient: a replay after the call settled would run the view's tool twice.
        # A surface that mounts while a call waits gets it again (register route).
        bus.publish(
            Event(
                type=APP_TOOL_CALL_EVENT,
                session_id=call.session_id,
                payload=call.event_payload(),
                transient=True,
            )
        )


# ---- the agent-facing tool -------------------------------------------------------


def _session() -> tuple[Any, str]:
    from clio_agent.gact import context as _ctx  # noqa: PLC0415

    app = _ctx.active_app()
    sid = _ctx.active_session_id() or _ctx.active_tool_session_id() or ""
    return app, sid


def _cancelled(app: Any) -> bool:
    from clio_agent.gact.cancellation_check import make_cancellation_checker  # noqa: PLC0415
    from clio_agent.runtime.cancellation import cancellation_requested  # noqa: PLC0415

    return cancellation_requested() or make_cancellation_checker(app)()


def _result_text(result: Mapping[str, Any] | None) -> str:
    blocks = (result or {}).get("content") or []
    texts = [
        str(block.get("text") or "")
        for block in blocks
        if isinstance(block, Mapping) and block.get("type") == "text"
    ]
    return "\n".join(t for t in texts if t).strip()


def _not_open_text(tool: AppTool, still_open: bool) -> str:
    view = f"the {tool.server} view"
    if still_open:
        return (
            f"{tool.agent_name} did not run: {view} no longer offers {tool.name}. "
            "Search the deferred tools again for what it offers now."
        )
    opening = f" Call {tool.opening_tool} to open it again" if tool.opening_tool else ""
    return f"{tool.agent_name} did not run: {view} is not open.{opening}"


def call_app_tool(agent_name: str, arguments: Mapping[str, Any]) -> str:
    """Run one view tool for the agent; the text blocks of the view's answer."""
    from clio_agent.gact.agents.tool_instrumentation import (  # noqa: PLC0415
        declare_structured_content,
    )

    app, sid = _session()
    if app is None or not sid:
        raise RuntimeError("A view tool can only run inside a session")
    table = app_tool_table(app)
    tool = table.offered(sid).get(agent_name)
    base = {"arguments": dict(arguments)}
    if tool is None:
        known = table.known(sid).get(agent_name)
        if known is None:
            raise RuntimeError(f"No open view offers {agent_name}")
        still_open = table.holds(sid, known.app_instance_id)
        text = _not_open_text(known, still_open)
        outcome = "not_offered" if still_open else "not_open"
        trace.event("APPS", "%s tool=%s outcome=%s", APP_TOOL_NOT_OPEN, agent_name, outcome)
        declare_structured_content({**base, **_identity(known), "outcome": outcome, "text": text})
        return text
    call = table.begin(tool, sid, arguments)
    publish_call(app, call)
    try:
        while not call.done.wait(0.25):  # no timeout (rule 6): only a cancel ends the wait
            if _cancelled(app):
                from clio_agent.gact.agents.clio_react import _turn_cancelled  # noqa: PLC0415

                raise _turn_cancelled()
    finally:
        table.forget(call)
    if call.error == APP_TOOL_NOT_OPEN:
        text = _not_open_text(tool, False)
        outcome = "not_open"
    elif call.error is not None:
        text = f"The {tool.server} view could not run {tool.name}: {call.error}"
        outcome = "view_error"
    else:
        text = _result_text(call.result) or f"The {tool.server} view ran {tool.name}."
        outcome = "view_error" if (call.result or {}).get("isError") else "returned"
        if outcome == "view_error":
            text = f"The {tool.server} view reported an error from {tool.name}: {text}"
    declare_structured_content({**base, **_identity(tool), "outcome": outcome, "text": text})
    return text


def _identity(tool: AppTool) -> dict[str, Any]:
    return {
        "server": tool.server,
        "tool": tool.name,
        "agent_name": tool.agent_name,
        "app_instance_id": tool.app_instance_id,
        "opening_tool": tool.opening_tool,
    }


def _signature(schema: Mapping[str, Any]) -> inspect.Signature | None:
    properties = schema.get("properties")
    properties = properties if isinstance(properties, Mapping) else {}
    required = set(schema.get("required") or ())
    names = [str(n) for n in properties]
    if not all(n.isidentifier() for n in names):
        return None
    params = [
        inspect.Parameter(
            n,
            inspect.Parameter.KEYWORD_ONLY,
            default=inspect.Parameter.empty if n in required else None,
        )
        for n in sorted(names, key=lambda n: n not in required)
    ]
    return inspect.Signature(params)


def _agent_tool(tool: AppTool) -> Any:
    from clio_agent.gact.agents.tool_instrumentation import (  # noqa: PLC0415
        instrument_tools,
        native_tool,
    )
    from clio_agent.gact.mcp_app_tool_presentation import present, starting  # noqa: PLC0415

    agent_name = tool.agent_name

    def run(**arguments: Any) -> str:
        return call_app_tool(agent_name, arguments)

    run.__name__ = agent_name
    run._clio_app_tool = tool  # type: ignore[attr-defined]  # what the build was made from
    signature = _signature(tool.input_schema)
    if signature is not None:
        run.__signature__ = signature  # type: ignore[attr-defined]
    properties = tool.input_schema.get("properties")
    args = {str(k): dict(v) for k, v in (properties or {}).items() if isinstance(v, Mapping)}
    about = tool.description.strip() or f"The {tool.name} tool of the view."
    built = native_tool(
        run,
        name=agent_name,
        desc=(
            f"{about} (A tool of the open {tool.server} view, which the person sees; "
            f"it acts on what the view shows.)"
        ),
        args=args,
        presentation=present,
        presentation_start=starting(tool.server, tool.name),
        domain="surfaces",
        title=tool.name.replace("_", " ").strip().capitalize(),
        read_only=tool.read_only,
    )
    return instrument_tools([built])[0]


# ---- placing them in the agent's tools and deferred catalog --------------------------


def _apply(tools: MutableMapping[str, Any], app: Any, sid: str) -> bool:
    """Make every app tool this session knows callable; return whether names changed."""
    known = app_tool_table(app).known(sid)
    offered = app_tool_table(app).offered(sid)
    changed = False
    for name, tool in known.items():
        current = offered.get(name, tool)
        built = getattr(tools.get(name), "func", None)
        if getattr(built, "_clio_app_tool", None) != current:
            tools[name] = _agent_tool(current)
            changed = True
    if changed and "submit" in tools:
        tools["submit"] = tools.pop("submit")  # the final-output tool stays last
    return changed


def place_app_tools(program: Any) -> list[str]:
    """Offered app tools join the deferred catalog; the rest leave it. Returns those added.

    A tool this session loaded and listed stays listed. Tools no view offers stay
    callable (unlisted) so a late call is answered with where the view went.
    """
    from clio_agent.gact.agents.deferred_tools import SEARCH_TOOLS  # noqa: PLC0415
    from clio_agent.gact.tool_catalog import CatalogEntry  # noqa: PLC0415

    app, sid = _session()
    catalog = getattr(program, "_clio_deferred_catalog", None)
    tools: Mapping[str, Any] = getattr(program, "tools", {}) or {}
    if app is None or not sid or catalog is None:
        return []
    offered = app_tool_table(app).offered(sid)
    loaded = frozenset(getattr(program, "_clio_loaded_listed", frozenset()) or ())
    app_names = {n for n in tools if n.startswith(APP_TOOL_PREFIX)}
    if not app_names:
        return []
    added: list[str] = []
    for name in app_names:
        tool = offered.get(name)
        if tool is None or name in loaded:
            if name in catalog:
                catalog.remove(name)
            continue
        description = getattr(tools[name], "desc", "") or tool.description
        if name in catalog:
            catalog.remove(name)
        else:
            added.append(name)
        catalog.add(CatalogEntry(name, tool.server, str(description), dict(tool.input_schema)))
    unlisted = set(getattr(program, "_clio_unlisted", frozenset()) or ())
    unlisted |= {n for n in app_names if not (n in offered and n in loaded)}
    if SEARCH_TOOLS in tools and len(catalog):
        unlisted.discard(SEARCH_TOOLS)  # app tools are behind it, so it is offered
    object.__setattr__(program, "_clio_unlisted", frozenset(unlisted))
    if added:
        trace.event("APPS", "mcp_app_tools_offered names=%s", ",".join(sorted(added)))
    return sorted(added)


def attach_app_tools(program: Any) -> list[str]:
    """At build: the session's app tools join the program and its deferred catalog."""
    app, sid = _session()
    tools = getattr(program, "tools", None)
    if app is None or not sid or tools is None:
        return []
    _apply(tools, app, sid)
    refresher = getattr(program, "_clio_tool_refresh", None)
    if isinstance(refresher, AppToolRefresher):
        refresher.seen(app_tool_table(app).revision)
    return place_app_tools(program)


class AppToolRefresher:
    """The agent loop's per-step refresh: a server's ``list_changed``, then app tools."""

    def __init__(self, inner: Any = None) -> None:
        self._inner = inner
        self._revision = -1

    def __deepcopy__(self, memo: dict[int, Any]) -> "AppToolRefresher":
        import copy  # noqa: PLC0415

        return copy.copy(self)

    def seen(self, revision: int) -> None:
        """The build already applied ``revision``."""
        self._revision = revision

    def __call__(self, tools: MutableMapping[str, Any]) -> bool:
        changed = bool(self._inner(tools)) if self._inner is not None else False
        app, sid = _session()
        if app is None or not sid:
            return changed
        revision = app_tool_table(app).revision
        if revision == self._revision:
            return changed
        self._revision = revision
        _apply(tools, app, sid)
        return True  # the catalog follows in sync_deferred_catalog


def app_tool_refresher(inner: Any = None) -> AppToolRefresher:
    """Wrap a built agent's namespace refresher so view tools follow too."""
    return AppToolRefresher(inner)
