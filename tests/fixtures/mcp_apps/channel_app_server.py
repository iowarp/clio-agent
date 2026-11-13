"""An MCP App server whose view reports a ``time`` state channel (plan AQ27-AQ29).

Built on the ext-apps ``basic-server-vanillajs`` pattern: one tool opens a
``ui://`` view, a resource serves that view, and a server tool sets the state
the view shows. The view reports its time step through
``ui/update-model-context`` whenever the person moves its slider, and can send
a burst of sixty reports in one second. ``set_time`` is the channel's tool: a
host calls it with ``{"time": value}`` when the time changes elsewhere.

Every ``set_time`` call is appended as one JSON line to the file named by
``CHANNEL_APP_LOG`` (when set), so a live check can count calls exactly.
``CHANNEL_APP_OPEN_DELAY_S`` (when set) makes ``open_timeline`` take that many
seconds after logging its start, so a check can act while an open is in flight.

Run: ``python tests/fixtures/mcp_apps/channel_app_server.py`` (stdio).
"""

from __future__ import annotations

import json
import os
import time
import time as pytime
from pathlib import Path

import mcp_types
from fastmcp import FastMCP
from fastmcp.apps import AppConfig
from fastmcp.tools.base import ToolResult

VIEW_URI = "ui://timeline/view"
VIEW_HTML = Path(__file__).with_name("channel_view.html")

_state = {"time": 0, "steps": 24}


def _log(row: dict[str, object]) -> None:
    path = os.environ.get("CHANNEL_APP_LOG")
    if path:
        with open(path, "a", encoding="utf-8") as stream:
            stream.write(json.dumps({**row, "at": time.time()}) + "\n")


def _state_result(text: str) -> ToolResult:
    return ToolResult(
        content=[mcp_types.TextContent(type="text", text=text)],
        structured_content=dict(_state),
    )


def build_server() -> FastMCP:
    """Build the channel fixture server."""

    server = FastMCP("timeline")

    @server.tool(app=AppConfig(resource_uri=VIEW_URI, visibility=["model", "app"]))
    def open_timeline(time: int = 0, steps: int = 24) -> ToolResult:
        """Open the timeline view at a time step (0 to steps - 1)."""

        _state["steps"] = max(1, min(int(steps), 1000))
        _state["time"] = max(0, int(time))
        _log({"tool": "open_timeline", "time": _state["time"], "steps": _state["steps"]})
        delay = float(os.environ.get("CHANNEL_APP_OPEN_DELAY_S") or 0)
        if delay > 0:
            pytime.sleep(delay)
            _log({"tool": "open_timeline", "finished": True})
        return _state_result(f"Timeline open at step {_state['time']} of {_state['steps']}.")

    @server.tool(app=AppConfig(visibility=["model", "app"]))
    def set_time(time: int) -> ToolResult:
        """Move the timeline view to a time step."""

        _state["time"] = int(time)
        _log({"tool": "set_time", "time": _state["time"]})
        return _state_result(f"Timeline moved to step {_state['time']}.")

    @server.resource(VIEW_URI, app=AppConfig())
    def timeline_view() -> str:
        """The timeline view."""

        return VIEW_HTML.read_text(encoding="utf-8")

    return server


if __name__ == "__main__":
    build_server().run()
