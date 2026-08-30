"""MCP server registry + dispatch routes for the GACT server (#714).

SPEC §6.7 third-party MCP server surface the gact-tui MCP browser and the
``/v1/mcp/servers/{sid}/call`` dispatch path consume:

* ``GET /v1/mcp/servers`` -- enumerate the bundled in-process built-ins plus any
  third-party servers installed at runtime (and disabled agent-blueprint MCP
  descriptors).
* ``GET /v1/mcp/handshake`` -- live readiness probe of every DECLARED MCP server
  (workspace/user ``mcp.yaml`` + active pack frontmatter), reachability + tool
  count per server.
* ``POST /v1/mcp/servers`` -- install + connect to a third-party server (stdio or
  http), record it on ``app.state.external_mcp_servers``.
* ``POST /v1/mcp/servers/{sid}/call`` -- invoke a tool on an installed server,
  firing the same permission gate + tool observer the agent uses.
* ``DELETE /v1/mcp/servers/{sid}`` -- uninstall a third-party server (gated by the
  shared direct-destructive-action permission guard).
* ``POST /v1/mcp/servers/{sid}/reconnect`` -- re-probe a previously-installed
  server's stored transport spec (timeout-bounded; non-destructive).
* ``GET /v1/mcp/servers/{sid}`` -- detail row for one server.
* ``GET /v1/mcp/servers/{sid}/(tools|resources|prompts)`` and ``POST .../prompts/get``
  -- detail enumeration plus protocol prompt fetches (bundled via the in-process
  gateway, external via a short-lived ``fastmcp.Client`` connection).

Handlers close over the ``app`` argument (FastAPI's decorators need it) and
read/write the live third-party registry via ``app.state.external_mcp_servers``.
The permission gate / tool observer are read from the constructors stored on
``app.state.make_permission_gate`` / ``app.state.make_tool_observer`` (DI seam),
the workspace-scoped catalog cwd via
:func:`~clio_agent.gact.agents.resolution._runtime_workspace_catalog_cwd`, and the
cross-concern destructive-action guard through
:class:`~clio_agent.gact.routes.deps.GactDeps`. The module imports only already
extracted gact packages (runtime globals, agents.resolution, agent_blueprints,
events, types) and never loads :mod:`clio_agent.gact.app`.
"""

from __future__ import annotations

import asyncio
import contextvars
import json
import uuid
from collections.abc import Mapping
from contextlib import nullcontext
from pathlib import Path
from typing import TYPE_CHECKING, Any, Literal, Optional

from fastapi import FastAPI, HTTPException, Request

from clio_agent.gact.agent_blueprints import discover_agent_blueprints, load_mcp_descriptors
from clio_agent.gact.agents.resolution import (
    _runtime_active_agent_blueprint_id,
    _runtime_active_agent_blueprint_path,
    _runtime_workspace_catalog_cwd,
)
from clio_agent.gact.blueprint_activation import blueprint_mcp_servers, blueprint_server_map
from clio_agent.gact.events import Event
from clio_agent.gact.mcp_apps import call_tool_result_to_observer
from clio_agent.gact.permission_gate import (
    _external_mcp_permission_context,
    _invoke_permission_gate,
    _normalize_mcp_tool_annotations,
)
from clio_agent.gact.routes._body import json_body
from clio_agent.gact.routes.mcp_rows import (
    bundled_server_tool_rows,
    mcp_inventory_row,
    mcp_prompt_result_row,
)
from clio_agent.gact.routes.mcp_server_specs import stdio_server_spec
from clio_agent.gact.runtime.globals import _tool_session_context
from clio_agent.gact.types import ErrorEnvelope, ErrorInfo
from clio_agent.tools.execution import notify_tool_observer
from clio_agent.tools.mcp_config import MCPTransportError, redact_mcp_spec, transport_from_spec
from clio_agent.tools.mcp_errors import typed_mcp_call_error

if TYPE_CHECKING:
    from clio_agent.gact.routes.deps import GactDeps


def _mcp_reconnect_timeout_s() -> float:
    """Return the MCP reconnect/probe timeout in seconds.

    Bounds the connect + ``list_tools`` round-trip in
    ``POST /v1/mcp/servers/{sid}/reconnect`` so a hung MCP server cannot
    block the route indefinitely. Defaults to 15s (a sensible ceiling for
    a stdio spawn + first tool listing) and is overridable via
    ``CLIO_GACT_MCP_RECONNECT_TIMEOUT_S``. A non-positive or unparseable
    value falls back to the 15s default rather than disabling the guard."""

    from clio_agent import conf  # noqa: PLC0415 - avoid import cycle at module load

    try:
        value = conf.resolve(
            "limits.mcp_reconnect_timeout_s",
            env="CLIO_GACT_MCP_RECONNECT_TIMEOUT_S",
            default=15.0,
            cast=conf.as_float,
        )
    except (ValueError, TypeError):
        return 15.0
    return value if value > 0 else 15.0


def _external_mcp_tool_annotations(info: Mapping[str, Any], tool_name: str) -> Any:
    """Return cached annotations for one external MCP tool, or ``None``.

    Runtime-installed servers retain their compact string tool list and keep
    annotations in a side mapping. Agent-blueprint servers use richer tool
    rows. Supporting both shapes keeps the existing registry/API contract while
    ensuring every external dispatch reaches the same fail-closed gate.
    """

    annotations_by_tool = info.get("tool_annotations")
    if isinstance(annotations_by_tool, Mapping) and tool_name in annotations_by_tool:
        return annotations_by_tool.get(tool_name)
    tools = info.get("tools")
    if not isinstance(tools, list):
        return None
    for tool in tools:
        if not isinstance(tool, Mapping):
            continue
        name = str(tool.get("name") or tool.get("id") or "")
        if name == tool_name:
            return tool.get("annotations")
    return None


def register_mcp_routes(app: FastAPI, deps: "GactDeps") -> None:
    """Register the SPEC §6.7 MCP server registry + dispatch routes on ``app``.

    Handlers close over the ``app`` argument (FastAPI's decorators need it) and
    read/write the third-party registry via ``app.state.external_mcp_servers``;
    the install/call/reconnect paths reach the permission gate + tool observer
    through the ``app.state.make_permission_gate`` / ``app.state.make_tool_observer``
    constructors and the destructive-action guard through ``deps``.
    """

    @app.get("/v1/mcp/servers")
    async def list_mcp_servers(workspace_id: str = "") -> dict[str, Any]:
        """SPEC §6.7 — enumerate MCP servers the backend has mounted.

        Returns BOTH the bundled in-process built-ins (fs/shell) AND any
        declared/third-party servers installed via POST /v1/mcp/servers.
        Each row carries id/name/status/transport/tools_count/tools.
        """

        rows = _mcp_server_rows(cwd=_runtime_workspace_catalog_cwd(app, workspace_id=workspace_id))
        return {"servers": rows}

    def _mcp_server_rows(cwd: Path | None = None) -> list[dict[str, Any]]:
        """Return bundled plus installed MCP server catalog rows."""
        rows: list[dict[str, Any]] = []
        # In-process bundled built-in servers (fs/shell via gateway).
        try:
            from clio_agent.tools.gateway import list_capabilities

            caps = list_capabilities()
            per_server: dict[str, list[dict[str, str]]] = {}
            for tool in caps:
                srv = tool.get("server", "unknown")
                per_server.setdefault(srv, []).append(tool)
            for name, tools in sorted(per_server.items()):
                rows.append(
                    {
                        "id": f"mcp_{name}",
                        "name": name,
                        "status": "ready",
                        "transport": "in_process",
                        "tools_count": len(tools),
                        "tools": [t["name"] for t in tools],
                    }
                )
        except Exception as exc:  # noqa: BLE001
            rows.append(
                {
                    "id": "mcp_bundled_error",
                    "name": "bundled-gateway",
                    "status": "error",
                    "transport": "in_process",
                    "tools_count": 0,
                    "tools": [],
                    "error": f"gateway introspection failed: {exc!r}",
                }
            )

        # Third-party servers installed at runtime.
        installed = getattr(app.state, "external_mcp_servers", {})
        installed_ids: set[str] = set()
        for sid, info in sorted(installed.items()):
            installed_ids.add(str(sid))
            rows.append(
                {
                    "id": sid,
                    "name": info.get("name", sid),
                    "status": info.get("status", "unknown"),
                    "transport": info.get("transport", "unknown"),
                    "tools_count": len(info.get("tools") or []),
                    "tools": list(info.get("tools") or []),
                    "spec": redact_mcp_spec(info.get("spec", {})),
                }
            )
        try:
            for blueprint in discover_agent_blueprints(cwd=cwd):
                for descriptor in load_mcp_descriptors(
                    blueprint.root,
                    scope=blueprint.scope,
                    blueprint_id=blueprint.id,
                ):
                    descriptor_server_id = f"agent_blueprint_mcp_{blueprint.id}_{descriptor['id']}"
                    legacy_descriptor_server_id = f"agent_blueprint_mcp_{descriptor['id']}"
                    if (
                        descriptor_server_id in installed_ids
                        or legacy_descriptor_server_id in installed_ids
                    ):
                        continue
                    rows.append(
                        {
                            "id": descriptor_server_id,
                            "name": descriptor["name"],
                            "status": "disabled",
                            "transport": descriptor.get("transport") or "unknown",
                            "tools_count": 0,
                            "tools": [],
                            "spec": descriptor,
                            "source": "agent_blueprint",
                            "agent_blueprint_id": blueprint.id,
                            "enabled": False,
                        }
                    )
        except Exception:  # noqa: BLE001,S110 - row assembly best-effort; partial rows returned
            pass
        return rows

    def _declared_mcp_specs(
        cwd: Path | None = None,
        *,
        session_id: str = "",
    ) -> dict[str, Any]:
        """Assemble the declared MCP server specs the runtime would mount.

        Mirrors ``ClioAgent._build_tool_gateway``: merge each active blueprint's
        ``mcp_servers`` frontmatter (pack scope) with user/workspace ``mcp.yaml``
        via ``load_mcp_servers``. Returns ``{name: MCPServerSpec}``; discovery
        failures degrade to the mcp.yaml-only set (best-effort), never raising.
        """
        from clio_agent.tools.mcp_config import load_mcp_servers  # noqa: PLC0415

        blueprint_id = _runtime_active_agent_blueprint_id(app, session_id)
        pack_servers: dict[str, dict[str, Any]] = {}
        if blueprint_id:
            blueprint_path = _runtime_active_agent_blueprint_path(app, session_id)
            if blueprint_path is not None:
                try:
                    from clio_agent.gact.agent_blueprints import (  # noqa: PLC0415
                        parse_agent_blueprint_root,
                    )

                    blueprint = parse_agent_blueprint_root(blueprint_path, scope="session")
                    if blueprint.enabled and blueprint.id == blueprint_id:
                        servers = blueprint_server_map(blueprint)
                        if servers:
                            pack_servers[blueprint_id] = servers
                except Exception:  # noqa: BLE001,S110 - mcp.yaml remains available
                    pass
            else:
                pack_servers = blueprint_mcp_servers(blueprint_id, cwd=cwd)
        return load_mcp_servers(cwd=cwd, pack_servers=pack_servers)

    @app.get("/v1/mcp/handshake")
    async def mcp_handshake(workspace_id: str = "", session_id: str = "") -> dict[str, Any]:
        """Live readiness handshake for every DECLARED MCP tool server.

        Complements ``GET /v1/mcp/servers`` (a catalog of what is mounted): this
        actively connects to each declared server (workspace/user ``mcp.yaml`` +
        active pack frontmatter) over its transport, lists its tools, and reports
        per-server reachability + tool count. That lets a client (the TUI) show
        "clio-kit up (12 tools), hdf5 **down**" instead of one aggregate gateway
        row. One unreachable/slow server never sinks the rest (probed in
        parallel; bounded per-server timeout).

        This is intentionally an on-demand endpoint, NOT part of ``/v1/health``:
        probing stdio servers spawns subprocesses (e.g. ``uvx``) and can take
        seconds, which would make the frequently-polled health check slow and
        flaky. The TUI calls this when it wants live tool-server status.
        """
        from clio_agent.gact.routes.mcp_rows import handshake_server_row  # noqa: PLC0415
        from clio_agent.providers.handshake import handshake_mcp_servers  # noqa: PLC0415

        cwd = _runtime_workspace_catalog_cwd(app, workspace_id=workspace_id, session_id=session_id)
        specs = _declared_mcp_specs(cwd=cwd, session_id=session_id)
        reports = await handshake_mcp_servers(list(specs.values()))
        return {"servers": [handshake_server_row(report) for report in reports]}

    @app.post("/v1/mcp/servers", status_code=201)
    async def install_mcp_server(request: Request) -> dict[str, Any]:
        """Install + connect to a third-party MCP server.

        Body shapes:
        - stdio:  {"name": "everything", "transport": "stdio",
                   "command": "npx", "args": ["-y", "@modelcontextprotocol/server-everything"],
                   "env": {...}}
        - http:   {"name": "remote", "transport": "http",
                   "url": "https://mcp.example.com"}

        Connects via fastmcp.Client, lists the server's tools, and
        records the server in ``app.state.external_mcp_servers`` so
        subsequent /v1/mcp/servers GETs and tool dispatch can see it.

        Returns the same row shape /v1/mcp/servers does.
        """

        body = await json_body(request, route="POST /v1/mcp/servers")
        name = body.get("name") or body.get("id") or "unnamed"
        transport_kind = (body.get("transport") or "stdio").lower()

        try:
            from fastmcp import Client
        except Exception as exc:  # noqa: BLE001 - safe exception type is returned
            raise HTTPException(
                status_code=503,
                detail=ErrorEnvelope(
                    error=ErrorInfo(
                        error="dependency_missing",
                        message=f"fastmcp Client unavailable (reason={type(exc).__name__})",
                        recoverable=False,
                    )
                ).model_dump(exclude_none=True),
            ) from None

        if transport_kind == "stdio":
            spec = stdio_server_spec(body)
            transport = transport_from_spec(spec)
        elif transport_kind in {"http", "streamable-http"}:
            url = body.get("url")
            if not url:
                raise HTTPException(
                    status_code=422,
                    detail=ErrorEnvelope(
                        error=ErrorInfo(
                            error="bad_request",
                            message="http transport requires 'url'",
                            recoverable=True,
                        )
                    ).model_dump(exclude_none=True),
                )
            transport = transport_from_spec({"transport": transport_kind, "url": url})
            spec = {"transport": "http", "url": url}
        else:
            raise HTTPException(
                status_code=422,
                detail=ErrorEnvelope(
                    error=ErrorInfo(
                        error="bad_request",
                        message=f"unknown transport: {transport_kind!r} (use stdio|http)",
                        recoverable=True,
                    )
                ).model_dump(exclude_none=True),
            )

        # Probe the server: connect, list tools, disconnect cleanly.
        # We re-create the Client per dispatch later (cheap for stdio,
        # no shared global state to worry about).
        tool_names: list[str] = []
        tool_annotations: dict[str, dict[str, Any] | None] = {}
        connect_error: Optional[str] = None
        try:
            async with Client(transport) as client:
                tools = await client.list_tools()
                tool_names = [t.name for t in tools]
                tool_annotations = {t.name: _normalize_mcp_tool_annotations(t) for t in tools}
        except Exception as exc:  # noqa: BLE001
            connect_error = repr(exc)

        sid = f"mcp_ext_{uuid.uuid4().hex[:10]}"
        if not hasattr(app.state, "external_mcp_servers"):
            app.state.external_mcp_servers = {}
        info = {
            "id": sid,
            "name": name,
            "status": "ready" if connect_error is None else "error",
            "transport": transport_kind,
            "tools": tool_names,
            "tool_annotations": tool_annotations,
            "spec": spec,
        }
        if connect_error:
            info["error"] = connect_error
        app.state.external_mcp_servers[sid] = info

        if connect_error is not None:
            raise HTTPException(
                status_code=502,
                detail=ErrorEnvelope(
                    error=ErrorInfo(
                        error="upstream_unavailable",
                        message=f"MCP server probe failed: {connect_error}",
                        details={"id": sid, "spec": redact_mcp_spec(spec)},
                        recoverable=True,
                    )
                ).model_dump(exclude_none=True),
            )
        return {
            "id": sid,
            "name": name,
            "status": "ready",
            "transport": transport_kind,
            "tools_count": len(tool_names),
            "tools": tool_names,
            "spec": redact_mcp_spec(spec),
        }

    @app.post("/v1/mcp/servers/{sid}/call")
    async def call_external_mcp_tool(sid: str, request: Request) -> dict[str, Any]:
        """Invoke a tool on an installed third-party MCP server.

        Body: {"tool": "<tool_name>", "args": {...}}

        Connects via fastmcp.Client using the spec recorded at
        install time, calls the tool, fires the same global
        tool_observer the agent uses (so SSE events + tools_called
        ledger entries land identically to in-process tools), and
        returns the structured result.
        """

        installed = getattr(app.state, "external_mcp_servers", {}) or {}
        info = installed.get(sid)
        if info is None:
            raise HTTPException(
                status_code=404,
                detail=ErrorEnvelope(
                    error=ErrorInfo(
                        error="not_found",
                        message=f"no installed MCP server: {sid}",
                        recoverable=True,
                    )
                ).model_dump(exclude_none=True),
            )
        body = await json_body(request, route="POST /v1/mcp/servers/{sid}/call")
        tool_name = body.get("tool")
        tool_args = body.get("args") or {}
        requested_session_id = str(body.get("session_id") or "").strip()
        if not tool_name:
            raise HTTPException(
                status_code=422,
                detail=ErrorEnvelope(
                    error=ErrorInfo(
                        error="bad_request",
                        message="missing 'tool' in request body",
                        recoverable=True,
                    )
                ).model_dump(exclude_none=True),
            )
        if requested_session_id and app.state.sessions.get(requested_session_id) is None:
            raise HTTPException(
                status_code=404,
                detail=ErrorEnvelope(
                    error=ErrorInfo(
                        error="not_found",
                        message=f"session not found: {requested_session_id}",
                        recoverable=True,
                    )
                ).model_dump(exclude_none=True),
            )

        observer_name = f"{info.get('name', 'ext')}.{tool_name}"
        tool_session = (
            _tool_session_context(requested_session_id) if requested_session_id else nullcontext()
        )
        with tool_session:
            gate = (
                getattr(app.state, "pending_permission_gate", None)
                or app.state.make_permission_gate()
            )
            permission_context = _external_mcp_permission_context(
                _external_mcp_tool_annotations(info, str(tool_name))
            )
            tool_context = contextvars.copy_context()
            try:
                decision = await asyncio.get_running_loop().run_in_executor(
                    None,
                    lambda: tool_context.run(
                        _invoke_permission_gate,
                        gate,
                        observer_name,
                        tool_args,
                        permission_context,
                    ),
                )
            except PermissionError as exc:
                raise HTTPException(
                    status_code=403,
                    detail=ErrorEnvelope(
                        error=ErrorInfo(
                            error="permission_error",
                            message=str(exc),
                            recoverable=True,
                        )
                    ).model_dump(exclude_none=True),
                ) from exc
            if decision != "allow":
                raise HTTPException(
                    status_code=403,
                    detail=ErrorEnvelope(
                        error=ErrorInfo(
                            error="permission_error",
                            message=f"tool call {observer_name!r} denied by permission gate",
                            recoverable=True,
                        )
                    ).model_dump(exclude_none=True),
                )

            # Build transport + client BEFORE tool-start: 422 bad spec / 503 missing dep (#1106).
            spec = info.get("spec", {})
            try:
                from clio_agent.tools.mcp_runtime import make_mcp_client  # noqa: PLC0415

                transport = transport_from_spec(spec)
                client_ctx = make_mcp_client(transport, server_id=sid)  # #1201: direct connect
            except MCPTransportError as exc:
                raise HTTPException(
                    status_code=422,
                    detail=ErrorEnvelope(
                        error=ErrorInfo(
                            error="mcp_spec_invalid",
                            message=str(exc),
                            details={"id": sid, "spec": redact_mcp_spec(spec)},
                            recoverable=True,
                        )
                    ).model_dump(exclude_none=True),
                ) from exc
            except ImportError as exc:
                raise HTTPException(
                    status_code=503,
                    detail=ErrorEnvelope(
                        error=ErrorInfo(
                            error="dependency_missing",
                            message=f"fastmcp Client unavailable (reason={type(exc).__name__})",
                            recoverable=False,
                        )
                    ).model_dump(exclude_none=True),
                ) from None

            # Fire tool observer manually so this call shows up in
            # tools_called + tool.call.* SSE events identically to an
            # agent-driven tool call. Same observer, no special path.
            tool_observer = getattr(app.state, "pending_tool_observer", None)
            if tool_observer is None:
                tool_observer = app.state.make_tool_observer()
            notify_tool_observer(tool_observer, observer_name, tool_args, "started")
            try:
                async with client_ctx as client:
                    result = await client.call_tool(tool_name, tool_args)
                content = []
                for c in getattr(result, "content", None) or []:
                    content.append(
                        {
                            "type": getattr(c, "type", "text"),
                            "text": getattr(c, "text", str(c)),
                        }
                    )
            except Exception as raw_exc:  # noqa: BLE001
                # #1114: typed translation first — no raw SDK class/message on the wire.
                surfaced = typed_mcp_call_error(raw_exc, tool=tool_name) or raw_exc
                notify_tool_observer(
                    tool_observer, observer_name, tool_args, "completed", error=repr(surfaced)
                )
                raise HTTPException(
                    status_code=502,
                    detail=ErrorEnvelope(
                        error=ErrorInfo(
                            error="upstream_error",
                            message=f"tool call failed: {surfaced}",
                            details=getattr(surfaced, "details", None) or {},
                            recoverable=True,
                        )
                    ).model_dump(exclude_none=True),
                ) from raw_exc
            tool_result_text = "\n".join(str(item.get("text", item)) for item in content)
            if not tool_result_text:
                data = getattr(result, "data", None)
                tool_result_text = (
                    json.dumps(data, sort_keys=True, default=str)
                    if isinstance(data, Mapping)
                    else str(data if data is not None else result)
                )
            notify_tool_observer(
                tool_observer,
                observer_name,
                tool_args,
                "completed",
                result=call_tool_result_to_observer(result),
            )
            return {
                "server_id": sid,
                "tool": tool_name,
                "args": tool_args,
                "content": content,
                "is_error": getattr(result, "isError", False),
                **({"session_id": requested_session_id} if requested_session_id else {}),
            }

    @app.delete("/v1/mcp/servers/{sid}", status_code=204)
    async def uninstall_mcp_server(sid: str) -> None:
        """Drop a third-party MCP server registration. Bundled
        in-process servers (mcp_fs/mcp_hdf5/mcp_parquet) cannot be
        removed at runtime — return 404 for those."""

        installed = getattr(app.state, "external_mcp_servers", {}) or {}
        if sid not in installed:
            raise HTTPException(
                status_code=404,
                detail=ErrorEnvelope(
                    error=ErrorInfo(
                        error="not_found",
                        message=f"no externally-installed MCP server: {sid}",
                        recoverable=True,
                    )
                ).model_dump(exclude_none=True),
            )
        deps.guard_direct_destructive_action(
            app,
            tool_name="gact.mcp_server.delete",
            args={"server_id": sid},
            summary=f"uninstall MCP server {sid}",
            reason="user_requested_mcp_server_delete",
        )
        installed.pop(sid, None)
        return None

    @app.post("/v1/mcp/servers/{sid}/reconnect")
    async def reconnect_mcp_server(sid: str) -> dict[str, Any]:
        """Re-probe a previously-installed external MCP server (#636/#523).

        fastmcp.Client connections are ephemeral (re-created per
        dispatch), so there is no persistent socket to tear down —
        reconnect simply re-opens the stored transport spec, re-lists the
        tools, and updates the registry row in place. Non-destructive, so
        (unlike DELETE) no permission guard. Bundled in-process servers
        (mcp_fs/mcp_hdf5/mcp_parquet) are not in the external registry, so
        they 404 here (they cannot be reconnected)."""

        installed = getattr(app.state, "external_mcp_servers", {}) or {}
        info = installed.get(sid)
        if info is None:
            raise HTTPException(
                status_code=404,
                detail=ErrorEnvelope(
                    error=ErrorInfo(
                        error="not_found",
                        message=f"no externally-installed MCP server: {sid}",
                        recoverable=True,
                    )
                ).model_dump(exclude_none=True),
            )

        try:
            from fastmcp import Client
        except Exception as exc:  # noqa: BLE001 - safe exception type is returned
            raise HTTPException(
                status_code=503,
                detail=ErrorEnvelope(
                    error=ErrorInfo(
                        error="dependency_missing",
                        message=f"fastmcp Client unavailable (reason={type(exc).__name__})",
                        recoverable=False,
                    )
                ).model_dump(exclude_none=True),
            ) from None

        # Validate the stored transport spec BEFORE touching the registry row or
        # attempting any connection: a malformed spec (stdio without a command,
        # http/sse without a url, unknown transport) is a client-actionable 4xx
        # (mcp_spec_invalid), so the row is never left half-updated.
        spec = info.get("spec") or {}
        transport_kind = str(spec.get("transport") or "").lower()

        def _spec_invalid(message: str) -> HTTPException:
            return HTTPException(
                status_code=422,
                detail=ErrorEnvelope(
                    error=ErrorInfo(
                        error="mcp_spec_invalid",
                        message=message,
                        details={
                            "id": sid,
                            "transport": transport_kind,
                            "spec": redact_mcp_spec(spec),
                        },
                        recoverable=True,
                    )
                ).model_dump(exclude_none=True),
            )

        try:
            transport: Any = transport_from_spec(spec)
        except MCPTransportError as exc:
            raise _spec_invalid(f"MCP server {sid} cannot reconnect: {exc}") from exc

        # Re-probe identically to install: open, list tools, close. The whole
        # connect + list-tools round-trip is bounded by a timeout so a hung
        # MCP server cannot hang the route.
        reconnect_timeout = _mcp_reconnect_timeout_s()
        tool_names: list[str] = []
        tool_annotations: dict[str, dict[str, Any] | None] = {}
        connect_error: Optional[str] = None
        timed_out = False

        async def _probe() -> tuple[list[str], dict[str, dict[str, Any] | None]]:
            async with Client(transport) as client:
                tools = await client.list_tools()
                return (
                    [t.name for t in tools],
                    {t.name: _normalize_mcp_tool_annotations(t) for t in tools},
                )

        try:
            tool_names, tool_annotations = await asyncio.wait_for(
                _probe(), timeout=reconnect_timeout
            )
        except (asyncio.TimeoutError, TimeoutError):
            timed_out = True
        except Exception as exc:  # noqa: BLE001
            connect_error = repr(exc)

        # On timeout, leave the registry row in a coherent error state — never
        # half-updated. Mark status="error" with a timeout message but preserve
        # the previously-known tool list (a hung probe tells us nothing new),
        # then surface the timeout to SSE clients and return a structured 504.
        if timed_out:
            timeout_msg = f"MCP server reconnect timed out after {reconnect_timeout:g}s"
            info["status"] = "error"
            info["error"] = timeout_msg
            installed[sid] = info  # tools untouched: registry stays consistent

            app.state.bus.publish(
                Event(
                    type="mcp.server.error",
                    session_id="",
                    payload={
                        "server_id": sid,
                        "name": info.get("name", ""),
                        "status": "error",
                        "error": timeout_msg,
                    },
                )
            )
            raise HTTPException(
                status_code=504,
                detail=ErrorEnvelope(
                    error=ErrorInfo(
                        error="mcp_reconnect_timeout",
                        message=timeout_msg,
                        details={"id": sid, "timeout_s": reconnect_timeout},
                        recoverable=True,
                    )
                ).model_dump(exclude_none=True),
            )

        # Update the registry row in place.
        info["status"] = "ready" if connect_error is None else "error"
        info["tools"] = tool_names
        info["tool_annotations"] = tool_annotations
        if connect_error:
            info["error"] = connect_error
        else:
            info.pop("error", None)
        installed[sid] = info

        if connect_error is not None:
            # Global status event (session_id="" like lm.provider.*).
            app.state.bus.publish(
                Event(
                    type="mcp.server.error",
                    session_id="",
                    payload={
                        "server_id": sid,
                        "name": info.get("name", ""),
                        "status": "error",
                        "error": connect_error,
                    },
                )
            )
            raise HTTPException(
                status_code=502,
                detail=ErrorEnvelope(
                    error=ErrorInfo(
                        error="upstream_unavailable",
                        message=f"MCP server reconnect failed: {connect_error}",
                        details={"id": sid, "spec": redact_mcp_spec(spec)},
                        recoverable=True,
                    )
                ).model_dump(exclude_none=True),
            )

        app.state.bus.publish(
            Event(
                type="mcp.server.reconnected",
                session_id="",
                payload={
                    "server_id": sid,
                    "name": info.get("name", ""),
                    "status": "ready",
                    "transport": info.get("transport", ""),
                    "tools": tool_names,
                },
            )
        )
        return {
            "id": sid,
            "name": info.get("name", ""),
            "status": "ready",
            "transport": info.get("transport", ""),
            "tools_count": len(tool_names),
            "tools": tool_names,
            "spec": redact_mcp_spec(spec),
        }

    @app.get("/v1/mcp/servers/{sid}")
    async def get_mcp_server(sid: str) -> dict[str, Any]:
        """SPEC §6.7 detail endpoint for one MCP server row."""

        for row in _mcp_server_rows():
            if row.get("id") == sid:
                return row
        raise HTTPException(
            status_code=404,
            detail=ErrorEnvelope(
                error=ErrorInfo(
                    error="not_found",
                    message=f"no MCP server: {sid}",
                    recoverable=False,
                )
            ).model_dump(exclude_none=True),
        )

    # ---- /v1/mcp/servers/{sid}/(tools|resources|prompts) ----------------
    # Detail enumeration for the TUI MCP browser: bundled servers introspect via the
    # in-process gateway, external servers via a short-lived fastmcp.Client connection.

    async def _external_mcp_inventory(
        sid: str,
        kind: Literal["tools", "resources", "prompts", "prompt"],
        *,
        prompt_name: str = "",
        arguments: dict[str, Any] | None = None,
    ) -> list[dict[str, Any]] | dict[str, Any]:
        """List an external inventory or fetch one rendered prompt."""
        installed = getattr(app.state, "external_mcp_servers", {}) or {}
        info = installed.get(sid)
        if info is None:
            raise HTTPException(
                status_code=404,
                detail=ErrorEnvelope(
                    error=ErrorInfo(
                        error="not_found",
                        message=f"no installed MCP server: {sid}",
                        recoverable=True,
                    )
                ).model_dump(exclude_none=True),
            )
        try:
            from fastmcp import Client

            from clio_agent.tools.mcp_connection_era import instrument_client_era
            from clio_agent.tools.mcp_runtime import make_mcp_client
        except Exception as exc:  # noqa: BLE001 - safe exception type is returned
            raise HTTPException(
                status_code=503,
                detail=ErrorEnvelope(
                    error=ErrorInfo(
                        error="dependency_missing",
                        message=f"fastmcp Client unavailable (reason={type(exc).__name__})",
                        recoverable=False,
                    )
                ).model_dump(exclude_none=True),
            ) from None
        spec = info.get("spec", {})
        try:
            transport = transport_from_spec(spec)
        except MCPTransportError as exc:
            raise HTTPException(
                status_code=422,
                detail=ErrorEnvelope(
                    error=ErrorInfo(
                        error="mcp_spec_invalid",
                        message=str(exc),
                        details={"id": sid, "spec": redact_mcp_spec(spec)},
                        recoverable=True,
                    )
                ).model_dump(exclude_none=True),
            ) from exc
        rows: list[dict[str, Any]] = []
        try:
            # #1201: a direct connect either way -- classify under both branches.
            client_context = (
                make_mcp_client(transport, server_id=sid)
                if kind == "prompt"
                else instrument_client_era(Client(transport), server_id=sid)
            )
            async with client_context as client:
                if kind == "tools":
                    items = await client.list_tools()
                elif kind == "resources":
                    items = await client.list_resources()
                elif kind == "prompts":
                    items = await client.list_prompts()
                else:
                    result = await client.get_prompt(prompt_name, arguments)
                    return mcp_prompt_result_row(result)
                rows.extend(mcp_inventory_row(item, kind=kind) for item in items)
        except Exception as exc:  # noqa: BLE001
            raise HTTPException(
                status_code=502,
                detail=ErrorEnvelope(
                    error=ErrorInfo(
                        error="upstream_error",
                        message=f"MCP {kind} {'fetch' if kind == 'prompt' else 'listing'} failed: {exc!r}",
                        recoverable=True,
                    )
                ).model_dump(exclude_none=True),
            ) from exc
        return rows

    @app.get("/v1/mcp/servers/{sid}/tools")
    async def get_mcp_tools(sid: str) -> dict[str, Any]:
        """List tools for an MCP server. Bundled servers report what the
        in-process gateway has registered; third-party servers connect
        via fastmcp.Client and call tools/list."""
        if sid.startswith("mcp_") and sid not in (
            getattr(app.state, "external_mcp_servers", {}) or {}
        ):
            return {"tools": bundled_server_tool_rows(sid[len("mcp_") :])}
        return {"tools": await _external_mcp_inventory(sid, "tools")}

    @app.get("/v1/mcp/servers/{sid}/resources")
    async def get_mcp_resources(sid: str) -> dict[str, Any]:
        """List resources for an MCP server. Bundled servers don't
        expose resources today (return empty); external servers query
        resources/list via fastmcp.Client."""
        if sid.startswith("mcp_") and sid not in (
            getattr(app.state, "external_mcp_servers", {}) or {}
        ):
            return {"resources": []}
        return {"resources": await _external_mcp_inventory(sid, "resources")}

    @app.get("/v1/mcp/servers/{sid}/prompts")
    async def get_mcp_prompts(sid: str) -> dict[str, Any]:
        """List prompts for an MCP server. Bundled servers don't expose
        prompts today (return empty); external servers query
        prompts/list via fastmcp.Client."""
        if sid.startswith("mcp_") and sid not in (
            getattr(app.state, "external_mcp_servers", {}) or {}
        ):
            return {"prompts": []}
        return {"prompts": await _external_mcp_inventory(sid, "prompts")}

    @app.post("/v1/mcp/servers/{sid}/prompts/get")
    async def get_mcp_prompt(sid: str, request: Request) -> dict[str, Any]:
        """Fetch and render one external MCP prompt via ``prompts/get``."""
        body = await json_body(request, route="POST /v1/mcp/servers/{sid}/prompts/get")
        prompt_name = body.get("name")
        arguments = body.get("arguments")
        if (
            not isinstance(prompt_name, str)
            or not prompt_name.strip()
            or (arguments is not None and not isinstance(arguments, dict))
        ):
            raise HTTPException(
                status_code=422,
                detail=ErrorEnvelope(
                    error=ErrorInfo(
                        error="bad_request",
                        message="'name' must be a non-empty string and 'arguments' an object",
                        recoverable=True,
                    )
                ).model_dump(exclude_none=True),
            )
        prompt = await _external_mcp_inventory(
            sid,
            "prompt",
            prompt_name=prompt_name.strip(),
            arguments=arguments,
        )
        return {"prompt": prompt}
