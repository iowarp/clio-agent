"""Whether a connected stdio MCP server's process has exited, for every stdio server.

``fastmcp.Client.is_connected`` only says a session object exists. It stays true after
the server's process exits, and every request then fails with ``Connection closed``
(``CONNECTION_CLOSED``) for as long as the client is kept. Each holder of a kept
connection asks :func:`server_exited` before using it and, when it is true, relaunches
the server through its normal launch path (the same client and transport).

The SDK's session dispatcher marks itself closed when its read loop reaches the end of
the server's stdout, and stdout ends when the process exits. That is one signal on
every OS: the pipe's write end closes with the process, so on POSIX the read returns
zero bytes and on Windows ``ReadFile`` reports the pipe broken, which the stream reads
as its end. A server that is alive but slow has not exited: waiting on it is the stall
reporter's job (:mod:`clio_agent.tools.mcp_server_progress`), never a deadline here.
"""

from __future__ import annotations

from typing import Any

#: A kept stdio connection's server process exited; it is started again in place.
MCP_STDIO_SERVER_EXITED = "mcp_stdio_server_exited"


def server_exited(client: Any) -> bool:
    """Whether ``client`` is a connected stdio client whose server process has exited."""
    from fastmcp.client.transports import StdioTransport  # noqa: PLC0415

    if client is None or not client.is_connected():
        return False
    transport = getattr(client, "transport", None)
    transport = getattr(transport, "original", transport)  # TaskObservationTransport
    if not isinstance(transport, StdioTransport):
        return False
    task = getattr(transport, "_connect_task", None)
    if task is not None and task.done():
        return True
    return bool(transport._is_session_dead())


async def relaunch(client: Any) -> None:
    """Start ``client``'s exited stdio server again on the same client and transport.

    The forced close resets the client's session and the transport's connection, so the
    re-entry spawns the server through the transport's own launch path (its rooted,
    declared command), outside the calling turn's context.
    """
    from clio_agent.tools.mcp_runtime import enter_connection  # noqa: PLC0415

    await client.close()
    await enter_connection(client)
