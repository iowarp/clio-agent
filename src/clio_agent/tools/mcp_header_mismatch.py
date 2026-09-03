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

from collections.abc import Sequence
from typing import Any, Protocol

from mcp.shared.exceptions import MCPError
from mcp_types.jsonrpc import HEADER_MISMATCH

from clio_agent.runtime import trace

__all__ = ["call_tool_with_header_retry", "trace_dropped_x_mcp_header_tools"]


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


async def trace_dropped_x_mcp_header_tools(
    client: Any, namespace: str, listed: Sequence[Any]
) -> None:
    """Emit a typed per-tool reason for any tool the SDK silently dropped for
    an invalid ``x-mcp-header`` annotation during THIS listing (#1285 review
    round SHOULD 3).

    The mcp SDK's own ``ClientSession._absorb_tool_listing`` already enforces
    the SEP-2578 MUST (a modern-era listing never carries an invalid-header
    tool) and logs the drop -- but only to the LIBRARY's own logger, never to
    CLIO's trace. This reconstructs what was dropped with ONE extra raw
    (pre-absorption) ``tools/list`` request for the FIRST PAGE ONLY: the SDK
    exposes no way to observe what its own absorption step ate, so going one
    layer below ``ClientSession.list_tools()`` -- straight to
    ``session.send_request`` -- is the only way to see the pre-drop set.

    Best-effort and diagnostic-only by design: any failure here (a legacy-era
    connection that never absorbs at all, a transport hiccup, a namespace
    whose tools span more than one page -- this probe does not paginate)
    degrades to silently skipping the diagnostic. It can NEVER affect the
    listing CLIO actually serves (``listed``, already resolved via the SDK's
    own fully-paginated, absorbed ``list_tools()`` before this is called),
    only the observability of what that resolution silently dropped.
    """

    from mcp_types.version import MODERN_PROTOCOL_VERSIONS  # noqa: PLC0415

    if getattr(client, "protocol_version", None) not in MODERN_PROTOCOL_VERSIONS:
        return

    try:
        import mcp_types  # noqa: PLC0415
        from mcp.shared.inbound import find_invalid_x_mcp_header  # noqa: PLC0415

        raw = await client.session.send_request(
            mcp_types.ListToolsRequest(params=None), mcp_types.ListToolsResult
        )
    except Exception as exc:  # noqa: BLE001 - diagnostic-only, never breaks the real listing
        trace.event(
            "TOOLS",
            "mcp_x_mcp_header_drop_probe_failed namespace=%s reason=%s",
            namespace,
            exc,
        )
        return

    kept_names = {getattr(tool, "name", None) for tool in listed}
    for tool in raw.tools:
        if tool.name in kept_names:
            continue
        reason = find_invalid_x_mcp_header(getattr(tool, "input_schema", None))
        if reason is not None:
            trace.event(
                "TOOLS",
                "mcp_x_mcp_header_dropped namespace=%s tool=%s reason=%s",
                namespace,
                tool.name,
                reason,
            )
