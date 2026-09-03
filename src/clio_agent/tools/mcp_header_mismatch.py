"""-32020 HeaderMismatch recovery for ``tools/call`` (SEP-2578, #1285 C1-S5).

``Mcp-Param-*`` headers are the mcp SDK's own responsibility (built-in to
``ClientSession._stamp``/``_resolve_param_headers`` off the tool's LAST listed
schema -- see ``mcp/client/session.py``). A server returns ``-32020
HeaderMismatch`` when the client's cached header map has gone stale relative
to what the server now expects (its schema changed since the client's last
``tools/list``); the spec's prescribed recovery is a fresh listing followed by
exactly ONE retry, never a terminal refusal like ``-32021``/``-32022``
(``tools/mcp_errors.py`` owns those -- deterministic, retrying can never
succeed). This module owns the ONE retryable code.
"""

from __future__ import annotations

from typing import Any, Protocol

from mcp.shared.exceptions import MCPError
from mcp_types.jsonrpc import HEADER_MISMATCH

from clio_agent.runtime import trace

__all__ = ["call_tool_with_header_retry"]


class _CallToolClient(Protocol):
    async def call_tool(self, name: str, arguments: dict[str, Any], **kwargs: Any) -> Any: ...

    async def list_tools(self) -> Any: ...


async def call_tool_with_header_retry(
    client: _CallToolClient,
    name: str,
    arguments: dict[str, Any],
    **call_kwargs: Any,
) -> Any:
    """Call ``name`` on ``client``, re-listing once and retrying on ``-32020``.

    Every other exception (including a SECOND ``-32020`` after the re-list)
    propagates unchanged -- the retry is a one-shot resynchronization, never a
    loop, so a server that keeps disagreeing with its own advertised schema
    surfaces the typed failure instead of hanging.

    Args:
        client: A connected MCP client exposing ``call_tool``/``list_tools``.
        name: The tool name to call.
        arguments: The tool's arguments.
        **call_kwargs: Forwarded verbatim to ``client.call_tool`` (e.g.
            ``progress_handler``).

    Returns:
        The tool call's result, from either the first attempt or the retry.
    """

    try:
        return await client.call_tool(name, arguments, **call_kwargs)
    except MCPError as exc:
        if exc.code != HEADER_MISMATCH:
            raise
        trace.event(
            "TOOLS",
            "mcp_header_mismatch_relist tool=%s message=%s",
            name,
            exc.message,
        )
        await client.list_tools()  # refreshes the session's Mcp-Param-* header map
        return await client.call_tool(name, arguments, **call_kwargs)
