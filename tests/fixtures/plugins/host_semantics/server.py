"""A plain MCP server that uses every host semantic a client may offer (HS1 to HS5).

Test-only. It imports fastmcp, never clio_agent, so it runs as the stdio server the
``host-fixture`` plugin declares, and tests also build it in process with
:func:`build_server`. Nothing in it is specific to one client:

- ``show_panel`` returns its A2UI as an embedded ``application/a2ui+json`` resource (HS1);
- ``bump`` is visible to apps only; a surface button calls it (HS2), and it tells the
  client the panel changed with ``notifications/resources/updated`` (HS3);
- ``a2ui://host-fixture/panel`` is the panel resource a tab reads (HS3);
- ``slow_bump`` is a required task whose terminal result carries A2UI;
- ``grant_status`` reads the account grant file the client names (HS4) and reports
  only whether one exists, never its token;
- ``open_link`` asks for a loopback URL through ``input_required`` (HS5).
"""

from __future__ import annotations

import argparse
import json
import os
from datetime import timedelta
from pathlib import Path
from typing import Any

import mcp_types
from fastmcp import Context, FastMCP
from fastmcp.apps import AppConfig
from fastmcp.tools.base import ToolResult
from fastmcp.utilities.tasks import TaskConfig
from fastmcp_tasks.extension import TasksExtension

PANEL_URI = "a2ui://host-fixture/panel"
A2UI_MIME = "application/a2ui+json"
BASIC_CATALOG = "https://a2ui.org/specification/v0_9/catalogs/basic/catalog.json"
SURFACE_ID = "hostfx-panel"
LOOPBACK_URL = "http://127.0.0.1:41234/confirm"
GRANT_ENV = "CLIO_ACCOUNT_GLOBUS_FILE"


def panel_messages(count: int, *, create: bool = True) -> list[dict[str, Any]]:
    """The panel as one A2UI batch: an optional ``createSurface``, then its components."""

    messages: list[dict[str, Any]] = []
    if create:
        messages.append(
            {
                "version": "v0.9.1",
                "createSurface": {"surfaceId": SURFACE_ID, "catalogId": BASIC_CATALOG},
            }
        )
    messages.append(
        {
            "version": "v0.9.1",
            "updateComponents": {
                "surfaceId": SURFACE_ID,
                "components": [
                    {"id": "root", "component": "Column", "children": ["count", "bump"]},
                    {"id": "count", "component": "Text", "text": f"Count: {count}"},
                    {"id": "bump-label", "component": "Text", "text": "Bump"},
                    {
                        "id": "bump",
                        "component": "Button",
                        "child": "bump-label",
                        "action": {"event": {"name": "bump", "context": {"by": 1}}},
                    },
                ],
            },
        }
    )
    return messages


def a2ui_block(messages: list[dict[str, Any]]) -> mcp_types.EmbeddedResource:
    return mcp_types.EmbeddedResource(
        resource=mcp_types.TextResourceContents(
            uri=PANEL_URI, mimeType=A2UI_MIME, text=json.dumps(messages)
        )
    )


def build_server(grant_file: str = "") -> FastMCP:
    """A fresh server; ``grant_file`` overrides ``$CLIO_ACCOUNT_GLOBUS_FILE``."""

    server = FastMCP("hostfx")
    server.add_extension(TasksExtension())
    state = {"count": 0}

    @server.resource(PANEL_URI, mime_type=A2UI_MIME)
    async def panel() -> str:
        """The panel's whole state."""

        return json.dumps(panel_messages(state["count"]))

    @server.tool
    async def show_panel() -> ToolResult:
        """Show the counter panel in the conversation."""

        return ToolResult(
            content=[
                mcp_types.TextContent(text=f"The count is {state['count']}."),
                a2ui_block(panel_messages(state["count"])),
            ]
        )

    @server.tool(app=AppConfig(visibility=["app"]))
    async def bump(ctx: Context, by: int = 1) -> ToolResult:
        """Add ``by`` to the count (called by the panel's button)."""

        state["count"] += int(by)
        await ctx.session.send_resource_updated(PANEL_URI)
        return ToolResult(
            content=[
                mcp_types.TextContent(text=f"Count is now {state['count']}."),
                a2ui_block(panel_messages(state["count"], create=False)),
            ]
        )

    @server.tool(task=TaskConfig(mode="required", poll_interval=timedelta(milliseconds=50)))
    async def slow_bump() -> ToolResult:
        """Add one to the count as a task; the result shows the panel."""

        state["count"] += 1
        return ToolResult(
            content=[
                mcp_types.TextContent(text=f"Count is now {state['count']}."),
                a2ui_block(panel_messages(state["count"])),
            ]
        )

    @server.tool
    async def grant_status() -> dict[str, Any]:
        """Whether an account grant file is present, and how many tokens it holds."""

        path = grant_file or os.environ.get(GRANT_ENV, "")
        try:
            document = json.loads(Path(path).read_text(encoding="utf-8")) if path else None
        except (OSError, ValueError):
            document = None
        tokens = (document or {}).get("tokens") or []
        return {"named": bool(path), "present": document is not None, "tokens": len(tokens)}

    @server.tool(task=TaskConfig(mode="required", poll_interval=timedelta(milliseconds=50)))
    async def open_link(ctx: Context) -> Any:
        """Ask the user to open a loopback page, then finish."""

        if not ctx.input_responses:
            return mcp_types.InputRequiredResult(
                inputRequests={
                    "q1": mcp_types.ElicitRequest(
                        params=mcp_types.ElicitRequestURLParams(
                            message="Confirm in the browser", url=LOOPBACK_URL
                        )
                    )
                },
                requestState="round-1",
                resultType="input_required",
            )
        return "confirmed"

    return server


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--grant", default="")
    build_server(parser.parse_args().grant).run()
