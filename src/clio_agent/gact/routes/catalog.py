"""Tool-catalog + slash-command routes for the GACT server (#714).

This concern exposes the two "what can this backend do" surfaces the TUI reads:

* ``GET /v1/catalog/tools`` -- the curated built-in tool catalog (SPEC §6.5/§6.6
  baseline used by the command palette).
* ``GET /v1/tools`` + ``GET /v1/tools/{tool_id}`` -- the unified live tool catalog:
  every tool the bundled in-process gateway and the installed third-party MCP
  servers expose, flattened with owner/tags/visibility metadata.

Shared catalog primitives live in :mod:`clio_agent.gact.catalog`. The slash-command
routes are :mod:`clio_agent.gact.routes.commands`.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import TYPE_CHECKING, Any

from fastapi import FastAPI, HTTPException

from clio_agent.gact.agents.tool_instrumentation import mcp_tool_title
from clio_agent.gact.catalog import (
    _tool_owner_for_catalog,
    _tool_tags_for_catalog,
    _tool_visible_to_for_catalog,
)
from clio_agent.gact.catalog_tool_schemas import builtin_tool_rows
from clio_agent.gact.routes.catalog_runtime_tools import agent_runtime_tool_rows
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
        return ListToolsResponse(tools=await builtin_tool_rows())

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
