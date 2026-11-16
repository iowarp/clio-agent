"""The native tools the ``builder`` plugin claims (besides ``make_plugin``).

``session_record`` writes named sessions' records for the agent to read in code;
``optimize_scope`` lists the sessions an ``/optimize`` would read and records them as
shown; ``optimize_report_shown`` moves the global watermark and keeps the findings the
person declined. Refusals are typed results (``reason``), never exceptions the model
has to parse.
"""

from __future__ import annotations

import asyncio
from typing import Any

from clio_agent.gact.builder import presentation
from clio_agent.gact.builder.records import write_records
from clio_agent.gact.builder.scope import ScopeRefused, build_scope, report_shown

#: Every builder tool name a plugin may claim (``make_plugin`` is registered on its own).
BUILDER_TOOLS: tuple[str, ...] = ("session_record", "optimize_scope", "optimize_report_shown")


def _active() -> tuple[Any, str]:
    from clio_agent.gact import context  # noqa: PLC0415

    app, session_id = context.active_app(), context.active_session_id()
    if app is None or not session_id:
        raise RuntimeError("the builder's tools need an active conversation")
    return app, session_id


def _refusal(exc: ScopeRefused, **extra: Any) -> dict[str, Any]:
    return {**extra, "reason": exc.reason, "error": str(exc), **exc.details}


async def session_record(sessions: list[str] | None = None) -> dict[str, Any]:
    app, session_id = _active()
    try:
        return await asyncio.to_thread(write_records, app, session_id, list(sessions or []))
    except ScopeRefused as exc:
        return _refusal(exc, written=False)


async def optimize_scope(
    plugin: str = "", sessions: list[str] | None = None, since: str = "", days: int = 0
) -> dict[str, Any]:
    app, session_id = _active()
    try:
        scope = await asyncio.to_thread(
            build_scope,
            app,
            session_id,
            plugin=plugin.strip(),
            sessions=list(sessions or []),
            since=since.strip(),
            days=int(days or 0),
        )
    except ScopeRefused as exc:
        return _refusal(exc, listed=False)
    return {"listed": True, **scope}


async def optimize_report_shown(declined: list[dict[str, Any]] | None = None) -> dict[str, Any]:
    app, session_id = _active()
    try:
        return await asyncio.to_thread(report_shown, app, session_id, list(declined or []))
    except ScopeRefused as exc:
        return _refusal(exc, recorded=False)


def _native(func: Any, presentation: Any, **declaration: Any) -> Any:
    from clio_agent.gact.agents.tool_instrumentation import native_tool  # noqa: PLC0415

    return native_tool(func, domain="agents", presentation=presentation, **declaration)


def build_session_record_tool() -> Any:
    return _native(
        session_record,
        name="session_record",
        title="Session record",
        desc=(
            "Write the full record of sessions (default: this conversation) into this "
            "conversation's scratch folder, children and flows included, and return each "
            "file's path with a summary (turns, tool calls, children, size). Read the files "
            "in run_code, not into context. Beside each, <id>.agent.json lists the plugins "
            "its agent carried. Only this conversation, sessions the person referenced with "
            "@ or by id, and the scope optimize_scope showed can be read (session_not_named)."
        ),
        args={
            "sessions": {
                "type": "array",
                "items": {"type": "string"},
                "default": [],
                "description": "Session ids; empty for this conversation.",
            }
        },
        presentation=presentation.session_record,
        presentation_start=presentation.session_record_start,
        read_only=True,
    )


def build_optimize_scope_tool() -> Any:
    return _native(
        optimize_scope,
        name="optimize_scope",
        title="Optimize scope",
        desc=(
            "List the root conversations an /optimize reads and record them as shown to "
            "the person. With plugin: the sessions where it ran (a tool or flow called, a "
            "skill loaded, a subagent started, its hooks active), last 90 days. Without: "
            "every conversation since the last global /optimize (the last 30 days the first "
            "time). sessions (named with @) or since/days override the window. Returns each "
            "session with its evidence, the window, and the findings the person declined "
            "before. Show the person the scope before reading any record."
        ),
        args={
            "plugin": {"type": "string", "default": "", "description": "A plugin id, or empty."},
            "sessions": {"type": "array", "items": {"type": "string"}, "default": []},
            "since": {"type": "string", "default": "", "description": "ISO date or time."},
            "days": {"type": "integer", "default": 0},
        },
        presentation=presentation.optimize_scope,
        read_only=True,
    )


def build_optimize_report_shown_tool() -> Any:
    return _native(
        optimize_report_shown,
        name="optimize_report_shown",
        title="Optimize report shown",
        desc=(
            "Call once the /optimize report is shown. A global /optimize moves its "
            "watermark to the end of the shown scope, so the next run starts there. "
            "declined lists the findings the person chose not to act on ({key, plugin, "
            "summary, sessions}); they are not proposed again unless they recur in new "
            "sessions."
        ),
        args={
            "declined": {
                "type": "array",
                "items": {"type": "object"},
                "default": [],
            }
        },
        presentation=presentation.optimize_report_shown,
    )


BUILDERS = {
    "session_record": build_session_record_tool,
    "optimize_scope": build_optimize_scope_tool,
    "optimize_report_shown": build_optimize_report_shown_tool,
}

__all__ = ["BUILDERS", "BUILDER_TOOLS"]
