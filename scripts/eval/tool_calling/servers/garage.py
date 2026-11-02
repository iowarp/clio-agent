"""A tiny server whose tool list grows mid-session (qualification T20).

``garage`` starts with ``status`` and ``unlock_controls``. The first
``unlock_controls`` call adds ``open_door`` and announces it with
``notifications/tools/list_changed``, as a real server does when a capability
becomes available. Its plugin declares it deferred, so the new tool joins the
deferred catalog and the plugin-change note (TD11) rather than the listed tools.

``panel`` is bound to an MCP App resource (``ui://garage/panel``), so its result
renders as an app view (qualification T13).
"""

from __future__ import annotations

from typing import Any

import mcp.types as mcp_types
from fastmcp import Context, FastMCP
from fastmcp.apps import AppConfig
from fastmcp.tools import Tool
from mcp.types import ToolAnnotations

try:  # run as a file from a plugin folder, or as a package module
    from toolkit import CallLog  # type: ignore[import-not-found]
except ImportError:  # pragma: no cover - package import
    from .toolkit import CallLog


def build(log: CallLog) -> FastMCP:
    """The garage server, logging each call to ``log``."""
    server = FastMCP("garage", instructions="Garage door controls.")
    state: dict[str, Any] = {"door": "closed", "unlocked": False}

    def open_door() -> dict[str, Any]:
        """Open the garage door (available once the controls are unlocked)."""
        log.record("open_door", {})
        state["door"] = "open"
        return {"door": state["door"]}

    @server.tool(annotations=ToolAnnotations(readOnlyHint=True))
    def status() -> dict[str, Any]:
        """Report whether the garage door is open and whether its controls are unlocked."""
        log.record("status", {})
        return {"door": state["door"], "controls_unlocked": state["unlocked"]}

    @server.tool
    async def unlock_controls(ctx: Context) -> dict[str, Any]:
        """Unlock the garage door controls; a tool to open the door becomes available."""
        log.record("unlock_controls", {})
        if not state["unlocked"]:
            state["unlocked"] = True
            server.add_tool(Tool.from_function(open_door, name="open_door"))
            await ctx.send_notification(mcp_types.ToolListChangedNotification())
        return {"controls_unlocked": True, "new_tools": ["open_door"]}

    @server.tool(app=AppConfig(resource_uri=PANEL_URI, visibility=["app", "model"]))
    def panel() -> dict[str, Any]:
        """Show the garage panel: the door and its controls, as an interactive view."""
        log.record("panel", {})
        return {"door": state["door"], "controls_unlocked": state["unlocked"]}

    @server.resource(PANEL_URI, app=AppConfig())
    def panel_view() -> str:
        """The MCP App shell the ``panel`` tool's result renders in."""
        return PANEL_HTML

    return server


PANEL_URI = "ui://garage/panel"
PANEL_HTML = """<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="color-scheme" content="light dark">
  <title>Garage panel</title>
  <style>
    :root { font: 14px/1.5 system-ui, sans-serif; }
    body { margin: 0; color: CanvasText; background: Canvas; }
    main { display: grid; gap: 8px; padding: 20px; }
  </style>
</head>
<body>
  <main>
    <h1>Garage</h1>
    <p id="door">Door: waiting for the tool result</p>
  </main>
  <script>
    window.addEventListener("message", (event) => {
      const data = event.data || {};
      const result = (data.params && (data.params.structuredContent || data.params.result)) || null;
      if (result && result.door) {
        document.getElementById("door").textContent =
          "Door: " + result.door + (result.controls_unlocked ? " (controls unlocked)" : "");
      }
    });
  </script>
</body>
</html>
"""
