"""Product external-MCP dispatch on the application loop and retained task transport."""

from __future__ import annotations

import asyncio
import json
import logging
import threading
from collections.abc import Mapping
from typing import Any

from clio_agent.gact import context as _ctx
from clio_agent.gact.permission_gate import (
    _external_mcp_permission_context,
    _invoke_permission_gate,
)

logger = logging.getLogger(__name__)


async def _call_enabled_external_mcp_tool(
    app: Any,
    server_id: str,
    info: Mapping[str, Any],
    tool_name: str,
    tool_args: Mapping[str, Any],
    tool_annotations: Any = None,
) -> str:
    """Call an explicitly enabled external MCP tool for a dynamic agent."""

    observer_name = f"{info.get('name', 'ext')}.{tool_name}"
    # Reach the permission gate via the active app's state (installed turn gate, else
    # the build_app-stored factory) rather than importing ``_make_permission_gate``
    # from ``gact.app`` -- keeps this module off a module-load cycle (#714 DI seam).
    gate = getattr(app.state, "pending_permission_gate", None)
    if gate is None:
        gate = app.state.make_permission_gate()
    decision = _invoke_permission_gate(
        gate,
        observer_name,
        dict(tool_args),
        _external_mcp_permission_context(tool_annotations),
    )
    if decision != "allow":
        raise PermissionError(f"tool call {observer_name!r} denied by permission gate")

    # Execution path (#1106 + #1113): this dynamic-agent call dispatches call_tool, so
    # its client comes from make_elicitation_client — the single factory PLUS the
    # elicitation handler bound to THIS call's invocation (one client per call).
    from clio_agent.gact.elicitation_bridge import make_elicitation_client  # noqa: PLC0415
    from clio_agent.gact.mcp_apps import call_tool_result_to_observer  # noqa: PLC0415
    from clio_agent.tools.execution import notify_tool_observer  # noqa: PLC0415
    from clio_agent.tools.mcp_config import (  # noqa: PLC0415
        MCPTransportError,
        transport_from_spec,
    )
    from clio_agent.tools.mcp_errors import typed_mcp_call_error  # noqa: PLC0415

    spec = info.get("spec", {})
    try:
        transport = transport_from_spec(spec)
        client_ctx = make_elicitation_client(app, transport, server_id, tool_name)
    except MCPTransportError:
        raise RuntimeError(f"unknown stored MCP transport for {server_id}") from None
    except (ImportError, RuntimeError, TypeError, ValueError, OSError) as exc:
        raise RuntimeError("fastmcp Client unavailable") from exc

    tool_observer = getattr(app.state, "pending_tool_observer", None)
    if tool_observer is None:
        tool_observer = app.state.make_tool_observer()
    notify_tool_observer(tool_observer, observer_name, dict(tool_args), "started")
    try:
        from clio_agent.gact.artifacts.observer_bridge import observer_call_id
        from clio_agent.tools.mcp_executor import AsyncMCPToolExecutor
        from clio_agent.tools.task_call_context import TASK_CALL, TaskCall

        executor = AsyncMCPToolExecutor(
            transport, client_factory=lambda _: client_ctx, server_id=server_id
        )
        token = TASK_CALL.set(
            TaskCall(invocation_id=observer_call_id(), owner_agent=_ctx.active_react_scope())
        )
        try:
            async with executor:
                outcome = await executor.call_tool_result(tool_name, dict(tool_args))
                result = outcome.raw_result
        finally:
            TASK_CALL.reset(token)
    except Exception as raw_exc:  # noqa: BLE001
        logger.exception("External MCP dispatch failed server=%s tool=%s", server_id, tool_name)
        # #1114: typed translation first — the model never sees a raw SDK class/message.
        surfaced = typed_mcp_call_error(raw_exc, tool=tool_name) or raw_exc
        notify_tool_observer(
            tool_observer, observer_name, dict(tool_args), "completed", error=repr(surfaced)
        )
        raise surfaced from raw_exc
    content = getattr(result, "content", None) or []
    result_text = "\n".join(str(getattr(part, "text", part)) for part in content)
    if not result_text:
        data = getattr(result, "data", None)
        result_text = (
            json.dumps(data, sort_keys=True, default=str)
            if isinstance(data, Mapping)
            else str(data if data is not None else result)
        )
    observer_result = call_tool_result_to_observer(result)
    notify_tool_observer(
        tool_observer,
        observer_name,
        dict(tool_args),
        "completed",
        # Legacy text projection for the model; the durable observer gets the
        # machine-readable public MCP result (private `_meta` stays excluded).
        result=observer_result,
    )
    if content:
        return result_text
    data = getattr(result, "data", None)
    if data is not None:
        return json.dumps(data, default=str) if isinstance(data, Mapping) else str(data)
    return str(result)


def _run_external_mcp_tool_sync(
    app: Any,
    server_id: str,
    info: Mapping[str, Any],
    tool_name: str,
    tool_args: Mapping[str, Any],
    tool_annotations: Any = None,
) -> str:
    application_loop = getattr(app.state, "mcp_app_loop", None)
    if application_loop is not None:
        try:
            running = asyncio.get_running_loop()
        except RuntimeError:
            running = None
        if running is application_loop:
            raise RuntimeError("Synchronous MCP tools must execute off the application loop")
        return asyncio.run_coroutine_threadsafe(
            _call_enabled_external_mcp_tool(
                app, server_id, info, tool_name, tool_args, tool_annotations
            ),
            application_loop,
        ).result()
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return asyncio.run(
            _call_enabled_external_mcp_tool(
                app,
                server_id,
                info,
                tool_name,
                tool_args,
                tool_annotations,
            )
        )

    result: dict[str, Any] = {}

    def _runner() -> None:
        try:
            result["value"] = asyncio.run(
                _call_enabled_external_mcp_tool(
                    app,
                    server_id,
                    info,
                    tool_name,
                    tool_args,
                    tool_annotations,
                )
            )
        except BaseException as exc:  # noqa: BLE001
            logger.exception(
                "External MCP compatibility loop failed server=%s tool=%s", server_id, tool_name
            )
            result["error"] = exc

    thread = threading.Thread(target=_runner, daemon=True)
    thread.start()
    thread.join()
    if "error" in result:
        raise result["error"]
    return str(result.get("value", ""))
