"""Closing MCP Apps through their declared cleanup tool, and telling the UI (MA7).

Moved out of :mod:`clio_agent.gact.mcp_apps` (its size ratchet). A record leaves the
registry when its view is closed, cleaned up, expired, or when its server stopped
(:mod:`clio_agent.gact.mcp_app_fleet`); :func:`publish_closed` tells the session's UI,
so a card or a tab whose app the server closed shows the app ended and offers Reopen
(MA19) instead of keeping a dead frame.
"""

from __future__ import annotations

import asyncio
import logging
from typing import TYPE_CHECKING, Any

from clio_agent.gact import view_call_cancel

if TYPE_CHECKING:
    from fastapi import FastAPI

    from clio_agent.gact.mcp_apps import MCPAppRecord

logger = logging.getLogger(__name__)

#: The session event a server-side close publishes: ``{session_id, app_instance_id, reason}``.
APP_CLOSED_EVENT = "mcp_app_closed"


def publish_closed(app: Any, record: Any, reason: str) -> None:
    """Tell ``record``'s session that the server closed its app (transient event)."""
    from clio_agent.gact.events import Event  # noqa: PLC0415

    bus = getattr(getattr(app, "state", None), "bus", None)
    if bus is None:
        return
    bus.publish(
        Event(
            type=APP_CLOSED_EVENT,
            session_id=record.session_id,
            payload={
                "session_id": record.session_id,
                "app_instance_id": record.app_instance_id,
                "reason": reason,
            },
            transient=True,
        )
    )


async def _cleanup_record(app: "FastAPI", record: "MCPAppRecord") -> None:
    """Run one declared App cleanup through the normal tool boundary."""

    from clio_agent.gact import mcp_apps  # noqa: PLC0415 - module cycle

    if record.cleanup is None:
        return
    cleanup_tool, cleanup_args = record.cleanup

    def cleanup(executor: Any) -> Any:
        full_name = mcp_apps._resolve_app_tool(executor, record, cleanup_tool)
        return executor.call_tool_result(full_name, cleanup_args)

    result = await mcp_apps._run_bound(app, record.session_id, cleanup)
    if mcp_apps.call_tool_result_to_wire(result).get("isError") is True:
        raise RuntimeError(f"MCP App cleanup tool {cleanup_tool!r} returned an error result")


def _schedule_rejected_cleanup(app: "FastAPI", record: "MCPAppRecord") -> None:
    """Immediately clean a post-call result whose UI admission was rejected.

    The compact cleanup record remains in the registry until this succeeds. If
    no running host loop is available, normal session/process teardown retries
    it instead of losing the ownership identity.
    """

    if record.cleanup is None:
        return
    loop = getattr(app.state, "mcp_app_loop", None)
    if not isinstance(loop, asyncio.AbstractEventLoop) or not loop.is_running():
        logger.error(
            "MCP App admission rejected; cleanup retained for teardown app_instance_id=%s",
            record.app_instance_id,
        )
        return

    from clio_agent.gact import mcp_apps  # noqa: PLC0415 - module cycle

    async def attempt() -> None:
        try:
            await _cleanup_record(app, record)
        except Exception:  # noqa: BLE001 - retain ownership and retry on teardown
            logger.exception(
                "MCP App rejected-admission cleanup failed; retained app_instance_id=%s",
                record.app_instance_id,
            )
        else:
            mcp_apps._registry(app).remove(record.app_instance_id)

    asyncio.run_coroutine_threadsafe(attempt(), loop)


async def cleanup_session_mcp_apps(app: "FastAPI", session_id: str) -> None:
    """Close every App owned by a session before destroying that session.

    Each cleanup runs through the same namespace routing, permission gate,
    observers, and session context as an ordinary tool call. Successfully
    closed records are removed immediately; failed records remain available so
    the operator can retry instead of silently leaking the remote attachment.
    """

    from clio_agent.gact import mcp_apps  # noqa: PLC0415 - module cycle
    from clio_agent.gact.mcp_app_service import close_app_service  # noqa: PLC0415
    from clio_agent.gact.mcp_app_tools import app_tool_table  # noqa: PLC0415

    registry = mcp_apps._registry(app)
    failures: list[str] = []
    app_tool_table(app).drop_session(session_id)
    for record in registry.records_for_session(session_id):
        try:
            await _cleanup_record(app, record)
        except Exception as exc:  # noqa: BLE001 - forwards: into the raised RuntimeError
            failures.append(f"{record.app_instance_id}: {exc}")
        else:
            registry.remove(record.app_instance_id)
            await close_app_service(app, record.app_instance_id)
    if failures:
        raise RuntimeError("; ".join(failures))


async def cleanup_all_mcp_apps(app: "FastAPI") -> None:
    """Close every retained App before the host tears down MCP transports."""

    from clio_agent.gact import mcp_apps  # noqa: PLC0415 - module cycle

    view_call_cancel.cancel_all(app)  # in-flight view calls end now, not with the process
    registry = mcp_apps._registry(app)
    failures: list[str] = []
    for session_id in registry.session_ids():
        try:
            await cleanup_session_mcp_apps(app, session_id)
        except RuntimeError as exc:
            failures.append(f"{session_id}: {exc}")
    if failures:
        raise RuntimeError("; ".join(failures))


__all__ = [
    "APP_CLOSED_EVENT",
    "_schedule_rejected_cleanup",
    "cleanup_all_mcp_apps",
    "cleanup_session_mcp_apps",
    "publish_closed",
]
