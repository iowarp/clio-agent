"""Tool-catalog + slash-command routes for the GACT server (#714).

This concern exposes the two "what can this backend do" surfaces the TUI reads,
plus the slash-command dispatch path:

* ``GET /v1/catalog/tools`` -- the curated built-in tool catalog (SPEC §6.5/§6.6
  baseline used by the command palette).
* ``GET /v1/tools`` + ``GET /v1/tools/{tool_id}`` -- the unified live tool catalog:
  every tool the bundled in-process gateway and the installed third-party MCP
  servers expose, flattened with owner/tags/visibility metadata.
* ``GET /v1/commands`` (#14) -- the backend slash-command catalog (built-ins +
  user/skill/agent-blueprint command files), optionally filtered to the
  planner-visible, agent-invocable subset.
* ``POST /v1/sessions/{sid}/commands/{cmd}`` -- dispatch one command: run a
  user/agent command's target agent, or apply a built-in side effect
  (``/clear`` / ``/cache-stats`` / ``/dump-trace``), then materialize a synthetic
  result message and audit row.

The catalog primitives (built-in tool list, per-tool owner/tags/visibility, the
on-disk command/skill loaders, the truthy-field coercion) live in the leaf
:mod:`clio_agent.gact.catalog` and are shared with the agent-run path. The
command-table assembly + dispatch helpers are concern-private and stay nested in
the factory (they close over ``app``/``deps``). The dispatch route reaches the
message-ledger primitives, the agent runner, and the destructive-action guard
through ``deps`` rather than importing back into :mod:`clio_agent.gact.app`.
"""

from __future__ import annotations

import asyncio
import contextvars
import json
import uuid
from collections.abc import Mapping
from datetime import datetime, timezone
from typing import TYPE_CHECKING, Any, Optional

from fastapi import FastAPI, HTTPException, Request

from clio_agent.gact.agents.tool_instrumentation import mcp_tool_title
from clio_agent.gact.catalog import (
    _builtin_tools,
    _tool_owner_for_catalog,
    _tool_tags_for_catalog,
    _tool_visible_to_for_catalog,
    _truthy_command_field,
)
from clio_agent.gact.events import Event
from clio_agent.gact.routes._body import json_body
from clio_agent.gact.routes.catalog_runtime_tools import agent_runtime_tool_rows
from clio_agent.gact.runtime.commands import (
    agent_allowed_command_ids,
    all_command_rows,
    command_context_for_request,
    command_index,
    planner_command_rows,
)
from clio_agent.gact.runtime.globals import (
    _active_semantic_turn_id,
    _emit_semantic_event,
    _gact_app_context,
)
from clio_agent.gact.runtime.retention import enforce_list_bound
from clio_agent.gact.types import (
    ErrorEnvelope,
    ErrorInfo,
    ListToolsResponse,
)
from clio_agent.tools.mcp_config import MCPTransportError, transport_from_spec

if TYPE_CHECKING:
    from clio_agent.gact.routes.deps import GactDeps


def register_catalog_routes(app: FastAPI, deps: "GactDeps") -> None:
    """Register the tool-catalog + slash-command routes on ``app``.

    Handlers close over the ``app`` argument (FastAPI's decorators need it) and
    reach sessions/agents/MCP-server registry through ``app.state``. The command
    table assembly + dispatch helpers stay nested here because they close over
    ``app`` (and the runtime blueprint resolvers that take ``app``); the message-
    ledger primitives, the agent runner, and the destructive-action guard travel
    on ``deps``.
    """

    @app.get("/v1/catalog/tools", response_model=ListToolsResponse)
    async def list_tools() -> ListToolsResponse:
        return ListToolsResponse(tools=_builtin_tools())

    @app.get("/v1/tools")
    async def list_tools_unified() -> dict[str, Any]:
        """SPEC §6.5 — unified tool catalog.

        Walks every MCP server the backend has mounted (bundled fs/
        hdf5/parquet via the in-process gateway, plus any third-party
        servers installed via POST /v1/mcp/servers) and returns a
        single flat list of tools. Each tool row carries:
        - id / name: the tool name (namespaced where the gateway
          namespaces them, e.g. "fs_read_file")
        - description: from the tool's docstring or schema
        - server_id / source: which MCP server exposes it
        - input_schema: JSON Schema (when available)
        """
        rows: list[dict[str, Any]] = []
        # Bundled in-process tools.
        try:
            from clio_agent.tools.gateway import list_gateway_tools  # noqa: PLC0415

            for tool in await list_gateway_tools():
                srv = tool.get("server", "")
                tool_name = tool.get("name", "")
                rows.append(
                    {
                        "id": tool_name,
                        "name": tool_name,
                        "title": mcp_tool_title(tool),
                        "description": tool.get("description") or "",
                        "server_id": f"mcp_{srv}" if srv else "",
                        "source": "mcp",
                        "input_schema": tool.get("input_schema") or {},
                        "output_schema": tool.get("output_schema") or {},
                        "owner": _tool_owner_for_catalog(tool_name),
                        "tags": _tool_tags_for_catalog(tool_name),
                        "visible_to": _tool_visible_to_for_catalog(tool_name),
                    }
                )
        except Exception as exc:  # noqa: BLE001
            rows.append(
                {
                    "id": "_bundled_error",
                    "name": "_bundled_error",
                    "description": f"bundled gateway introspection failed: {exc!r}",
                    "source": "error",
                }
            )

        # Third-party installed servers — query each via fastmcp.Client.
        installed = getattr(app.state, "external_mcp_servers", {}) or {}
        if installed:
            try:
                from fastmcp import Client  # noqa: PLC0415
            except Exception:  # noqa: BLE001
                Client = None  # type: ignore
            for sid, info in sorted(installed.items()):
                for declared in info.get("tools") or []:
                    if not isinstance(declared, Mapping):
                        continue
                    tool_name = str(declared.get("name") or declared.get("id") or "").strip()
                    if not tool_name:
                        continue
                    rows.append(
                        {
                            "id": tool_name,
                            "name": tool_name,
                            "title": str(declared.get("title") or ""),
                            "description": declared.get("description") or "",
                            "server_id": sid,
                            "source": "agent_blueprint_mcp_descriptor",
                            "status": declared.get("status") or info.get("status") or "unknown",
                            "enabled": bool(declared.get("enabled", False)),
                            "input_schema": declared.get("input_schema") or {},
                            "output_schema": declared.get("output_schema") or {},
                            "owner": _tool_owner_for_catalog(tool_name),
                            "tags": _tool_tags_for_catalog(tool_name),
                            "visible_to": _tool_visible_to_for_catalog(tool_name),
                            "agent_blueprint_id": info.get("agent_blueprint_id") or "",
                            "descriptor_id": info.get("descriptor_id") or "",
                        }
                    )
                spec = info.get("spec", {})
                if Client is None:
                    continue
                try:
                    transport = transport_from_spec(spec)
                except MCPTransportError as exc:
                    # No-silent-fallback: surface the unusable stored spec as a
                    # structured error row instead of dropping the server.
                    rows.append(
                        {
                            "id": f"{sid}_error",
                            "name": f"{sid}_error",
                            "description": f"invalid MCP transport spec for {sid}: {exc}",
                            "server_id": sid,
                            "source": "error",
                        }
                    )
                    continue
                try:
                    async with Client(transport) as client:
                        tools = await client.list_tools()
                    for t in tools:
                        tool_name = t.name
                        rows.append(
                            {
                                "id": tool_name,
                                "name": tool_name,
                                "title": mcp_tool_title(t),
                                "description": getattr(t, "description", "") or "",
                                "server_id": sid,
                                "source": "mcp",
                                "input_schema": getattr(t, "input_schema", None)
                                or getattr(t, "inputSchema", None)
                                or {},
                                "output_schema": getattr(t, "output_schema", None)
                                or getattr(t, "outputSchema", None)
                                or {},
                                "owner": _tool_owner_for_catalog(tool_name),
                                "tags": _tool_tags_for_catalog(tool_name),
                                "visible_to": _tool_visible_to_for_catalog(tool_name),
                            }
                        )
                except Exception as exc:  # noqa: BLE001
                    rows.append(
                        {
                            "id": f"{sid}_error",
                            "name": f"{sid}_error",
                            "description": f"failed to list {sid} tools: {exc!r}",
                            "server_id": sid,
                            "source": "error",
                        }
                    )
        # The model executor owns MCPs declared through workspace ``mcp.yaml``
        # and blueprint frontmatter. They are not part of the process-global
        # bundled gateway or manually installed GACT registry above, but they
        # are live model-callable tools and belong in this unified catalog.
        # Prefer the richer row already collected when registrations overlap.
        known_names = {str(row.get("name") or "") for row in rows if isinstance(row, Mapping)}
        rows.extend(row for row in agent_runtime_tool_rows(app) if row["name"] not in known_names)
        return {"tools": rows}

    @app.get("/v1/tools/{tool_id}")
    async def get_tool_detail(tool_id: str) -> dict[str, Any]:
        """SPEC §6.6 — single-tool detail. The TUI's tool-detail
        modal calls this when the user opens a row from the /tools
        catalog. Walks the same source as list_tools_unified() and
        returns the matching row, or 404 if no tool registers under
        ``tool_id``."""

        # Bundled in-process tools first — cheap.
        try:
            from clio_agent.tools.gateway import list_gateway_tools  # noqa: PLC0415

            for tool in await list_gateway_tools():
                if tool.get("name") == tool_id:
                    srv = tool.get("server", "")
                    return {
                        "id": tool_id,
                        "name": tool_id,
                        "title": mcp_tool_title(tool),
                        "description": tool.get("description") or "",
                        "server_id": f"mcp_{srv}" if srv else "",
                        "source": "mcp",
                        "input_schema": tool.get("input_schema") or {},
                        "output_schema": tool.get("output_schema") or {},
                        "owner": _tool_owner_for_catalog(tool_id),
                        "tags": _tool_tags_for_catalog(tool_id),
                        "visible_to": _tool_visible_to_for_catalog(tool_id),
                    }
        except Exception:  # noqa: BLE001,S110 - catalog enrichment best-effort; partial rows returned
            pass

        for row in agent_runtime_tool_rows(app):
            if row["name"] == tool_id:
                return row

        # Fall back to installed third-party MCP servers — heavier
        # because each lookup spawns a Client; cache could come later.
        installed = getattr(app.state, "external_mcp_servers", {}) or {}
        if installed:
            try:
                from fastmcp import Client  # noqa: PLC0415
            except Exception:  # noqa: BLE001 - optional fastmcp client; None when unavailable
                Client = None  # type: ignore
            for sid, info in installed.items():
                for declared in info.get("tools") or []:
                    if not isinstance(declared, Mapping):
                        continue
                    tool_name = str(declared.get("name") or declared.get("id") or "").strip()
                    if tool_name == tool_id:
                        return {
                            "id": tool_id,
                            "name": tool_id,
                            "title": str(declared.get("title") or ""),
                            "description": declared.get("description") or "",
                            "server_id": sid,
                            "source": "agent_blueprint_mcp_descriptor",
                            "status": declared.get("status") or info.get("status") or "unknown",
                            "enabled": bool(declared.get("enabled", False)),
                            "input_schema": declared.get("input_schema") or {},
                            "output_schema": declared.get("output_schema") or {},
                            "owner": _tool_owner_for_catalog(tool_id),
                            "tags": _tool_tags_for_catalog(tool_id),
                            "visible_to": _tool_visible_to_for_catalog(tool_id),
                            "agent_blueprint_id": info.get("agent_blueprint_id") or "",
                            "descriptor_id": info.get("descriptor_id") or "",
                        }
                if Client is None:
                    break
                try:
                    # Unify onto the spec-based helper (single canonical accepted
                    # set) instead of the old top-level ``info['transport']`` shape.
                    t = transport_from_spec(info.get("spec") or {})
                    async with Client(t) as cli:
                        tools = await cli.list_tools()
                    for tt in tools:
                        if getattr(tt, "name", "") == tool_id:
                            return {
                                "id": tool_id,
                                "name": tool_id,
                                "title": mcp_tool_title(tt),
                                "description": getattr(tt, "description", "") or "",
                                "server_id": sid,
                                "source": "mcp",
                                "input_schema": getattr(tt, "input_schema", None)
                                or getattr(tt, "inputSchema", None)
                                or {},
                                "output_schema": getattr(tt, "output_schema", None)
                                or getattr(tt, "outputSchema", None)
                                or {},
                                "owner": _tool_owner_for_catalog(tool_id),
                                "tags": _tool_tags_for_catalog(tool_id),
                                "visible_to": _tool_visible_to_for_catalog(tool_id),
                            }
                except Exception:  # noqa: BLE001 - per-server catalog probe failure skipped
                    continue

        raise HTTPException(
            status_code=404,
            detail=ErrorEnvelope(
                error=ErrorInfo(
                    error="not_found",
                    message=f"tool not found: {tool_id}",
                    recoverable=False,
                )
            ).model_dump(exclude_none=True),
        )

    # ---- /v1/commands + dispatch (#14) --------------------------------
    # The command-table assembly (built-ins + user/agent/blueprint command files,
    # planner-visible filtering) lives in the leaf runtime/commands.py so this
    # route and app.py's prompt-render-context closure share one source. The
    # prompt-render / argument-validation / audit helpers below are dispatch-
    # private and stay nested (they close over ``app``/``deps``).

    def _render_command_prompt(
        command_meta: Mapping[str, Any],
        *,
        user_input: str,
        args: Any,
        cmd_id: str,
        agent_id: str,
    ) -> str:
        prompt_template = str(command_meta.get("prompt_template") or "")
        if not prompt_template:
            return user_input or str(command_meta.get("description") or cmd_id)
        rendered = (
            prompt_template.replace("{{input}}", user_input)
            .replace("{{args}}", user_input)
            .replace("$ARGUMENTS", user_input)
            .replace("{{command}}", cmd_id)
            .replace("{{agent_id}}", agent_id)
        )
        if isinstance(args, Mapping):
            for key, value in args.items():
                rendered = rendered.replace(f"{{{{args.{key}}}}}", str(value))
        return rendered

    def _command_required_argument_names(command_meta: Mapping[str, Any]) -> list[str]:
        specs = command_meta.get("arguments") or []
        if isinstance(specs, str):
            return [specs] if specs.strip() else []
        if not isinstance(specs, list):
            return []
        required: list[str] = []
        for spec in specs:
            if isinstance(spec, str) and spec.strip():
                required.append(spec.strip())
            elif isinstance(spec, Mapping) and _truthy_command_field(
                spec.get("required"),
                False,
            ):
                name = str(spec.get("name") or spec.get("id") or "").strip()
                if name:
                    required.append(name)
        return required

    def _validate_command_arguments(
        command_meta: Mapping[str, Any],
        *,
        args: Any,
        user_input: str,
        cmd_id: str,
    ) -> None:
        required = _command_required_argument_names(command_meta)
        if not required:
            return
        if not isinstance(args, Mapping):
            if user_input:
                return
            missing = required
        else:
            missing = [name for name in required if args.get(name) in (None, "")]
        if not missing:
            return
        raise HTTPException(
            status_code=422,
            detail=ErrorEnvelope(
                error=ErrorInfo(
                    error="invalid_arguments",
                    message=f"command {cmd_id} is missing required arguments",
                    details={
                        "command": cmd_id,
                        "missing": missing,
                        "argument_hint": command_meta.get("argument_hint", ""),
                    },
                    recoverable=True,
                )
            ).model_dump(exclude_none=True),
        )

    def _command_audit_row(
        *,
        sid: str,
        cmd_id: str,
        command_meta: Mapping[str, Any],
        caller_type: str,
        caller_agent_id: str,
        args: Any,
        status: str,
        result_text: str = "",
        error: str = "",
    ) -> dict[str, Any]:
        row = {
            "id": f"cmd_audit_{uuid.uuid4().hex[:10]}",
            "session_id": sid,
            "command": cmd_id,
            "caller_type": caller_type,
            "caller_agent_id": caller_agent_id,
            "target_agent": str(command_meta.get("agent_id") or ""),
            "args": args if isinstance(args, Mapping) else {},
            "status": status,
            "result": result_text,
            "error": error,
            "command_source": str(
                command_meta.get("command_source") or command_meta.get("source") or ""
            ),
            "command_path": str(command_meta.get("command_path") or ""),
            "created_at": datetime.now(timezone.utc).isoformat(),
        }
        app.state.command_audit.append(row)
        enforce_list_bound(app, app.state.command_audit, "command_audit", session_id=sid)
        event_status = status if status in {"completed", "failed", "denied"} else "completed"
        _emit_semantic_event(
            app,
            sid,
            f"command.invocation.{event_status}",
            status=event_status,
            summary=f"Command {cmd_id} {event_status}.",
            actor={"caller_type": caller_type, "caller_agent_id": caller_agent_id},
            subject={
                "command": cmd_id,
                "command_audit_id": row["id"],
                "command_source": row["command_source"],
            },
            payload=row,
        )
        return row

    @app.get("/v1/commands")
    async def list_commands(
        agent_id: Optional[str] = None,
        session_id: Optional[str] = None,
        workspace_id: Optional[str] = None,
        planner: bool = False,
    ) -> dict[str, Any]:
        """SPEC §6.13 — backend-provided slash commands."""

        cwd, extra_roots = command_context_for_request(app, session_id or "", workspace_id or "")
        if planner:
            return {
                "commands": planner_command_rows(
                    app,
                    deps.resolve_runtime_dynamic_agent,
                    agent_id=agent_id or "",
                    cwd=cwd,
                    extra_roots=extra_roots,
                    session_id=session_id or "",
                )
            }
        return {"commands": all_command_rows(app, cwd=cwd, extra_roots=extra_roots)}

    @app.post("/v1/sessions/{sid}/commands/{cmd}")
    async def dispatch_command(sid: str, cmd: str, request: Request) -> dict[str, Any]:
        """Dispatch a backend command for a session. Returns a
        system-style result the TUI can render inline as a message.
        """

        sess = app.state.sessions.get(sid)
        if sess is None:
            raise HTTPException(
                status_code=404,
                detail=ErrorEnvelope(
                    error=ErrorInfo(
                        error="not_found",
                        message=f"session not found: {sid}",
                        recoverable=False,
                    )
                ).model_dump(exclude_none=True),
            )

        # Accept "clear" or "/clear"; the TUI sends both shapes.
        cmd_id = cmd if cmd.startswith("/") else "/" + cmd
        cwd, extra_roots = command_context_for_request(app, sid)
        commands_by_id = command_index(all_command_rows(app, cwd=cwd, extra_roots=extra_roots))
        command_meta = commands_by_id.get(cmd_id)
        if command_meta is None:
            raise HTTPException(
                status_code=404,
                detail=ErrorEnvelope(
                    error=ErrorInfo(
                        error="not_found",
                        message=f"unknown command: {cmd_id}",
                        details={"known": sorted(commands_by_id)},
                        recoverable=True,
                    )
                ).model_dump(exclude_none=True),
            )

        if command_meta.get("status") != "available" or command_meta.get("enabled") is False:
            raise HTTPException(
                status_code=501,
                detail=ErrorEnvelope(
                    error=ErrorInfo(
                        error=str(command_meta.get("error") or "unavailable"),
                        message=(
                            f"Backend command {cmd_id} is unavailable: "
                            f"{command_meta.get('disabled_reason') or command_meta.get('error')}"
                        ),
                        details={
                            "command": cmd_id,
                            "status": command_meta.get("status"),
                            "disabled_reason": command_meta.get("disabled_reason", ""),
                            "recovery_actions": [
                                "retry_after_optimizer_support_lands",
                                "exit",
                            ],
                        },
                        recoverable=False,
                    )
                ).model_dump(exclude_none=True),
            )

        request_body = await json_body(request, route="POST /v1/sessions/{sid}/commands/{cmd}")
        caller = request_body.get("caller")
        caller_meta = caller if isinstance(caller, Mapping) else {}
        caller_type = str(caller_meta.get("type") or request_body.get("caller_type") or "user")
        caller_agent_id = str(
            caller_meta.get("agent_id")
            or caller_meta.get("expert_id")
            or request_body.get("caller_agent_id")
            or ""
        ).strip()
        if caller_type == "agent":
            caller_agent = deps.resolve_runtime_dynamic_agent(
                caller_agent_id,
                session_id=sid,
                workspace_id=sess.workspace_id,
            )
            allowed_ids = agent_allowed_command_ids(caller_agent) if caller_agent else set()
            deny_reason = ""
            if command_meta.get("agent_invocable") is not True:
                deny_reason = "command is not agent-invocable"
            elif caller_agent is None:
                deny_reason = f"caller agent not found: {caller_agent_id}"
            elif cmd_id not in allowed_ids:
                deny_reason = f"command {cmd_id} is not allowed for agent {caller_agent_id}"
            if deny_reason:
                audit = _command_audit_row(
                    sid=sid,
                    cmd_id=cmd_id,
                    command_meta=command_meta,
                    caller_type=caller_type,
                    caller_agent_id=caller_agent_id,
                    args=request_body.get("args") or {},
                    status="denied",
                    error=deny_reason,
                )
                raise HTTPException(
                    status_code=403,
                    detail=ErrorEnvelope(
                        error=ErrorInfo(
                            error="command_denied",
                            message=deny_reason,
                            details={
                                "command": cmd_id,
                                "caller_type": caller_type,
                                "caller_agent_id": caller_agent_id,
                                "audit": audit,
                            },
                            recoverable=True,
                        )
                    ).model_dump(exclude_none=True),
                )

        if command_meta.get("source") == "user":
            agent_id = str(command_meta.get("agent_id") or "")
            agent_def = deps.resolve_runtime_dynamic_agent(
                agent_id,
                session_id=sid,
                workspace_id=sess.workspace_id,
            )
            if agent_def is None:
                raise HTTPException(
                    status_code=404,
                    detail=ErrorEnvelope(
                        error=ErrorInfo(
                            error="not_found",
                            message=f"command agent not found: {agent_id}",
                            details={"command": cmd_id, "agent_id": agent_id},
                            recoverable=True,
                        )
                    ).model_dump(exclude_none=True),
                )
            args = request_body.get("args")
            if args is None:
                args = request_body.get("arguments")
            user_input = str(
                request_body.get("input")
                or request_body.get("text")
                or request_body.get("prompt")
                or ""
            ).strip()
            if not user_input and args not in (None, ""):
                if isinstance(args, str):
                    user_input = args
                elif isinstance(args, Mapping) and len(args) == 1:
                    user_input = str(next(iter(args.values())))
                else:
                    user_input = json.dumps(args, sort_keys=True, default=str)
            try:
                _validate_command_arguments(
                    command_meta,
                    args=args,
                    user_input=user_input,
                    cmd_id=cmd_id,
                )
            except HTTPException as exc:
                if caller_type == "agent":
                    _command_audit_row(
                        sid=sid,
                        cmd_id=cmd_id,
                        command_meta=command_meta,
                        caller_type=caller_type,
                        caller_agent_id=caller_agent_id,
                        args=args if isinstance(args, Mapping) else {},
                        status="failed",
                        error="invalid_arguments",
                    )
                raise exc
            question = _render_command_prompt(
                command_meta,
                user_input=user_input,
                args=args,
                cmd_id=cmd_id,
                agent_id=agent_id,
            )
            # #755: the runner is a full (blocking) DSPy agent turn that can
            # take minutes. Run it off the event loop so /health, SSE
            # heartbeats, and other sessions stay responsive, mirroring how
            # turn.py executes blueprint runners. Copy the request context
            # with the app contextvar bound so the runner's dynamic-agent
            # tool wrappers still resolve ``app`` inside the worker thread;
            # the synchronous response shape and audit rows are unchanged.
            with _gact_app_context(app):
                command_turn_context = contextvars.copy_context()
            pred = await asyncio.get_running_loop().run_in_executor(
                None,
                lambda: command_turn_context.run(
                    deps.blueprint_runner_for_agent(agent_def),
                    app.state.agent,
                    agent_def,
                    question,
                    sid,
                ),
            )
            agent_body_text = str(getattr(pred, "answer", "") or "").strip()
            if not agent_body_text:
                agent_body_text = f"user command {cmd_id} completed with no answer"
            audit = _command_audit_row(
                sid=sid,
                cmd_id=cmd_id,
                command_meta=command_meta,
                caller_type=caller_type,
                caller_agent_id=caller_agent_id,
                args=args if isinstance(args, Mapping) else {},
                status="completed",
                result_text=agent_body_text,
            )

            from clio_agent.gact.types import Message, Part, Tokens  # noqa: PLC0415

            sys_msg = Message(
                id=f"msg_cmd_{uuid.uuid4().hex[:10]}",
                turn_id=_active_semantic_turn_id(),
                session_id=sid,
                role="assistant",
                created_at=datetime.now(timezone.utc).isoformat(),
                updated_at=datetime.now(timezone.utc).isoformat(),
                parts=[
                    Part(
                        id=f"part_cmd_{uuid.uuid4().hex[:10]}",
                        type="text",
                        agent_id=agent_id,
                        metadata={
                            "synthetic": "command_result",
                            "command": cmd_id,
                            "agent_id": agent_id,
                            "caller_type": caller_type,
                            "caller_agent_id": caller_agent_id,
                            "command_audit_id": audit["id"],
                        },
                        text=agent_body_text,
                    )
                ],
                tokens=Tokens(input=0, output=0, cache_read=0, cache_write=0),
                cost_usd=0.0,
                stop_reason="end_turn",
                metadata={
                    "synthetic": "command_result",
                    "command": cmd_id,
                    "agent_id": agent_id,
                    "route_source": "user_command",
                    "caller_type": caller_type,
                    "caller_agent_id": caller_agent_id,
                    "command_audit": audit,
                },
            )
            deps.append_session_message(app, sid, sys_msg)
            app.state.sessions.update(sid, message_count=len(app.state.messages.get(sid, [])))
            app.state.bus.publish(
                Event(
                    type="message.created",
                    session_id=sid,
                    payload=sys_msg.to_wire(),
                )
            )
            return {
                "command": cmd_id,
                "session_id": sid,
                "result": {
                    "type": "agent_message",
                    "text": agent_body_text,
                    "agent_id": agent_id,
                    "audit": audit,
                },
            }

        # Side effects + system message body per command.
        body_text: str
        if cmd_id == "/clear":
            deps.guard_direct_destructive_action(
                app,
                session_id=sid,
                workspace_id=sess.workspace_id,
                tool_name="gact.session.clear",
                args={"session_id": sid, "command": cmd_id},
                summary=f"clear session messages for {sid}",
                reason="user_requested_session_clear",
            )
            deps.delete_session_messages(app, sid)
            app.state.sessions.update(sid, message_count=0)
            app.state.bus.publish(
                Event(
                    type="session.cleared",
                    session_id=sid,
                    payload={"session_id": sid},
                )
            )
            body_text = "session messages cleared"
        elif cmd_id == "/cache-stats":
            stats: dict[str, Any] = {}
            if app.state.arc is not None:
                try:
                    stats = app.state.arc.get_cache_stats() or {}
                except Exception as exc:
                    raise HTTPException(
                        status_code=500,
                        detail=ErrorEnvelope(
                            error=ErrorInfo(
                                error="command_error",
                                message=(
                                    "Backend command /cache-stats could not read ARC "
                                    "cache statistics."
                                ),
                                details={
                                    "command": cmd_id,
                                    "original_error": str(exc),
                                    "recovery_actions": [
                                        "retry",
                                        "reconfigure_provider",
                                        "exit",
                                    ],
                                },
                                recoverable=True,
                            )
                        ).model_dump(exclude_none=True),
                    ) from exc
            body_text = (
                f"ARC cache: hits={stats.get('hits', 0)} "
                f"misses={stats.get('misses', 0)} "
                f"hit_rate={stats.get('hit_rate', 0.0):.2f} "
                f"capacity={stats.get('capacity', 0)}"
            )
        elif cmd_id == "/loop":  # P4.1 #1079: start an autonomous loop (owner module)
            from clio_agent.gact.autonomous_loop import run_loop_command  # noqa: PLC0415

            body_text = run_loop_command(app, sid, request_body)
        elif cmd_id == "/goal":  # P4.2 #1080: arm/clear a run-until goal (owner module)
            from clio_agent.gact.goal import run_goal_command  # noqa: PLC0415

            body_text = run_goal_command(app, sid, request_body)
        elif cmd_id in ("/cron", "/schedule"):  # P4.3 #1081: cron triad (owner module)
            from clio_agent.gact.cron_tools import run_cron_command  # noqa: PLC0415

            body_text = run_cron_command(app, sid, request_body)
        elif cmd_id == "/dump-trace":
            log = app.state.messages.get(sid, [])
            last_asst = next((m for m in reversed(log) if m.role == "assistant"), None)
            if last_asst is None:
                body_text = "no assistant turns yet"
            else:
                trace_part = next((p for p in last_asst.parts if p.type == "thinking"), None)
                body_text = (
                    trace_part.text
                    if trace_part is not None
                    else "no thinking trace on the last turn"
                )
        else:  # pragma: no cover - guarded above
            body_text = f"unhandled command: {cmd_id}"

        # Materialise body_text as a real assistant message so the TUI shows the result
        # (its runCommandCmd discards the POST response); persist + publish so SSE and GET
        # /messages reflect it.
        from clio_agent.gact.types import Message, Part, Tokens  # noqa: PLC0415

        sys_msg = Message(
            id=f"msg_cmd_{uuid.uuid4().hex[:10]}",
            turn_id=_active_semantic_turn_id(),
            session_id=sid,
            role="assistant",
            created_at=datetime.now(timezone.utc).isoformat(),
            updated_at=datetime.now(timezone.utc).isoformat(),
            parts=[
                Part(
                    id=f"part_cmd_{uuid.uuid4().hex[:10]}",
                    type="text",
                    # Built-in commands are executed by the orchestrator itself.
                    agent_id="main",
                    metadata={"synthetic": "command_result", "command": cmd_id},
                    text=f"[{cmd_id}] {body_text}",
                )
            ],
            tokens=Tokens(input=0, output=0, cache_read=0, cache_write=0),
            cost_usd=0.0,
            stop_reason="end_turn",
            metadata={"synthetic": "command_result", "command": cmd_id},
        )
        _emit_semantic_event(
            app,
            sid,
            "command.invocation.completed",
            status="completed",
            summary=f"Built-in command {cmd_id} completed.",
            actor={"caller_type": "user"},
            subject={"command": cmd_id, "message_id": sys_msg.id},
            payload={
                "command": cmd_id,
                "result": body_text,
                "command_source": str(command_meta.get("source") or ""),
            },
        )
        deps.append_session_message(app, sid, sys_msg)
        app.state.bus.publish(
            Event(
                type="message.created",
                session_id=sid,
                payload=sys_msg.to_wire(),
            )
        )

        return {
            "command": cmd_id,
            "session_id": sid,
            "result": {
                "type": "system_message",
                "text": body_text,
            },
        }
