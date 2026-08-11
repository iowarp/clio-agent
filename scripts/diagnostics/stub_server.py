"""Slow-starting stdio MCP stub server for the #1201 detectability probe.

Sleeps ``PROBE_STARTUP_DELAY_S`` seconds (default 0 -- no delay) BEFORE
starting the real FastMCP stdio server, simulating the "cold uv env /
matplotlib import / launcher cache-lock contention" spawn delay the #1186
race comment (``mcp_runtime.py``) describes: the client's outgoing bytes sit
in the OS pipe buffer, unread, until this process actually starts its event
loop and begins reading stdin. A fully protocol-conformant FastMCP server
otherwise -- the point of the probe is to observe NEGOTIATION TIMING, not to
hand-run a hand-rolled/possibly-nonconformant JSON-RPC responder.
"""

from __future__ import annotations

import os
import time

delay = float(os.environ.get("PROBE_STARTUP_DELAY_S", "0") or "0")
if delay > 0:
    time.sleep(delay)

from fastmcp import FastMCP  # noqa: E402

server = FastMCP("probe-1201-stub")


@server.tool
def echo(text: str) -> str:
    """Return ``text`` unchanged -- proves a real call reached this process."""
    return text


if __name__ == "__main__":
    server.run()
