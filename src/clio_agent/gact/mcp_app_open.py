"""Open an MCP App from a call an A2UI surface names (MA20).

A surface's ``clio.mcp-app.v1`` component carries the call that opens its view:
a server, one of its tools and the arguments, which is the saved call of MA18.
Showing the surface replays that call through the reopen path
(:func:`clio_agent.gact.mcp_app_calls.replay_call`): the same gated executor
call, the same admission, the same typed reasons. The call is not saved on the
session, because its definition already lives in the surface, and its view is
not added to the transcript, because it is mounted in the surface.

The answer also lists the server's tools the view may call, so the surface can
send a state channel's tool to the server or, when the server has no such
tool, to the view's own app tools.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import TYPE_CHECKING, Any

from fastapi import FastAPI, HTTPException, Request

from clio_agent.gact.mcp_app_calls import _ReplayCapture, replay_call
from clio_agent.gact.mcp_apps import _not_found, _run_bound, _tool_visible_to_app
from clio_agent.gact.routes._body import json_body

if TYPE_CHECKING:
    from clio_agent.gact.routes.deps import GactDeps

#: Recorded as ``reopened_from`` in the audit of an open a surface asked for.
SURFACE_OPEN = "surface"


def server_tools(definitions: Mapping[str, Any], server: str) -> list[str]:
    """The tools of ``server`` its views may call, without the server's prefix."""

    prefix = f"{server}_"
    return sorted(
        name[len(prefix) :]
        for name, tool in definitions.items()
        if name.startswith(prefix) and _tool_visible_to_app(tool)
    )


async def open_app_from_call(app: FastAPI, sid: str, call: Mapping[str, Any]) -> dict[str, Any]:
    """Make ``call`` and admit its view without saving the call or adding a card.

    Returns ``{part, saved_call, server_tools}`` or ``{error: {reason, detail},
    saved_call}``: the reopen answer plus the server's app-callable tools.
    """

    capture = _ReplayCapture(reopened_from=SURFACE_OPEN, display_mode="inline", save=False)
    outcome = await replay_call(app, sid, call, capture)
    if "part" in outcome:
        server = str(call.get("server") or "")
        definitions = await _run_bound(
            app, sid, lambda executor: executor.get_all_tool_definitions()
        )
        outcome["server_tools"] = server_tools(definitions, server)
    return outcome


def register_mcp_app_open_route(app: FastAPI, deps: GactDeps) -> None:
    """``POST /v1/sessions/{sid}/mcp-apps/open``: open a view from a call."""

    del deps

    @app.post("/v1/sessions/{sid}/mcp-apps/open")
    async def open_mcp_app(sid: str, request: Request) -> dict[str, Any]:
        if app.state.sessions.get(sid) is None:
            raise _not_found()
        body = await json_body(request, route="POST MCP App open")
        server = str(body.get("server") or "").strip()
        tool = str(body.get("tool") or "").strip()
        arguments = body.get("arguments") or {}
        if not server or not tool or not isinstance(arguments, Mapping):
            raise HTTPException(
                status_code=422, detail="server, tool and object arguments are required"
            )
        call = {"server": server, "tool": tool, "arguments": dict(arguments)}
        return await open_app_from_call(app, sid, call)


__all__ = ["SURFACE_OPEN", "open_app_from_call", "register_mcp_app_open_route", "server_tools"]
