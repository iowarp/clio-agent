"""A board view that reports context and offers its own tools (AQ18 to AQ22 fixture).

Built on the ext-apps ``basic-server-vanillajs`` pattern: one tool (``open_board``)
whose definition carries ``_meta.ui.resourceUri``, and one ``ui://`` resource holding
the view's HTML (``view_tools_view.html`` beside this file). The view speaks the MCP
Apps postMessage protocol by hand, with no SDK, so the fixture needs no build step.

The view:

* reports what it shows through ``ui/update-model-context`` whenever it changes,
  including the reading the person selected by clicking it;
* declares ``appCapabilities.tools`` and answers ``tools/list`` and ``tools/call``:
  ``set_highlight`` and ``set_color`` at first; after its first tool call it drops
  ``set_color``, adds ``add_note`` and sends ``notifications/tools/list_changed``.

Run it as a stdio MCP server: ``python tests/fixtures/mcp_apps/view_tools_server.py``.
"""

from __future__ import annotations

from pathlib import Path

from fastmcp import FastMCP
from fastmcp.apps import AppConfig
from fastmcp.tools.base import ToolResult
from mcp import types as mcp_types

VIEW_URI = "ui://board/view"
_VIEW = Path(__file__).with_name("view_tools_view.html")

#: The readings the board opens with. Plain fixture data.
READINGS = [
    {"id": "inlet", "label": "Inlet temperature", "value": "18.4 °C", "state": "normal"},
    {"id": "outlet", "label": "Outlet temperature", "value": "26.9 °C", "state": "normal"},
    {"id": "pump-b", "label": "Pump B vibration", "value": "7.9 mm/s", "state": "alarm"},
    {"id": "flow", "label": "Loop flow", "value": "41 L/min", "state": "low"},
]

server = FastMCP("board")


@server.tool(app=AppConfig(resource_uri=VIEW_URI, visibility=["app", "model"]))
def open_board(topic: str = "Cooling loop") -> ToolResult:
    """Open the readings board for a topic in a view the person can see and use."""
    return ToolResult(
        content=[mcp_types.TextContent(type="text", text=f"Opened the {topic} board.")],
        structured_content={"topic": topic, "readings": READINGS},
    )


@server.resource(VIEW_URI, app=AppConfig())
def board_view() -> str:
    """The board view's HTML."""
    return _VIEW.read_text(encoding="utf-8")


if __name__ == "__main__":
    server.run()
