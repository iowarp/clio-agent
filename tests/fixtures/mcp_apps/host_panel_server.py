"""An MCP Apps fixture server for the app host scenarios (AQ13-AQ17, AQ23, AQ24).

Modelled on ext-apps ``examples/basic-server-vanillajs``: one tool opens a view
(``ui://host-panel/panel``) and one plain tool the view and the agent can call.
The view (``host_panel.html``, self-contained) requests full screen, reports
model context, opens an https and a ``file:`` link and asks for a download.

Run over stdio: ``python tests/fixtures/mcp_apps/host_panel_server.py``.
"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

from fastmcp import FastMCP
from fastmcp.apps import AppConfig
from fastmcp.tools.base import ToolResult
from mcp import types as mcp_types

VIEW_URI = "ui://host-panel/panel"
VIEW_HTML = Path(__file__).with_name("host_panel.html")

server = FastMCP("host-panel")


@server.tool(app=AppConfig(resource_uri=VIEW_URI, visibility=["app", "model"]))
def open_panel(topic: str = "demo", link: str = "https://example.org/") -> ToolResult:
    """Open the host feature panel, an interactive view, for ``topic``.

    ``link`` is the web address the panel's "Open https link" button opens.
    """

    opened_at = datetime.now(UTC).isoformat(timespec="seconds")
    return ToolResult(
        content=[mcp_types.TextContent(type="text", text=f"Opened the panel for {topic}.")],
        structured_content={"topic": topic, "opened_at": opened_at},
    )


@server.tool(app=AppConfig(visibility=["app", "model"]))
def ping(note: str = "") -> str:
    """Answer with the server's time; the panel calls it to prove it is live."""

    return f"pong {note} at {datetime.now(UTC).isoformat(timespec='seconds')}".strip()


@server.resource(VIEW_URI, app=AppConfig())
def panel() -> str:
    """The panel's self-contained HTML."""

    return VIEW_HTML.read_text(encoding="utf-8")


if __name__ == "__main__":
    server.run()
