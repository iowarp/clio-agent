"""Prepare explicitly enabled blueprint MCP descriptors without enabling new ones."""

from __future__ import annotations

import asyncio
from copy import deepcopy
from pathlib import Path
from typing import Any

from anyio import BrokenResourceError, EndOfStream
from fastmcp.exceptions import ClientError, FastMCPError, MCPError
from httpx import HTTPError

from clio_agent.gact.agent_blueprints import load_mcp_descriptors, parse_agent_blueprint_root
from clio_agent.gact.blueprint_identity import identity_fields, select_blueprint


def store_enabled_descriptor(
    app: Any, server_id: str, row: dict[str, Any], root: Path, expected_checksum: str
) -> None:
    """Serialize explicit enablement against Reload and reject stale probe results."""
    from clio_agent.gact.blueprint_install_files import tree_checksum
    from clio_agent.gact.blueprint_mutations import BLUEPRINT_MUTATION_LOCK
    from clio_agent.gact.blueprint_source_configuration import SourceConfigurationConflict

    with BLUEPRINT_MUTATION_LOCK:
        if expected_checksum and tree_checksum(root) != expected_checksum:
            raise SourceConfigurationConflict(
                "Blueprint changed during MCP setup; retry on its current revision"
            )
        app.state.external_mcp_servers[server_id] = row


def _spec(descriptor: dict[str, Any], previous: dict[str, Any]) -> dict[str, Any]:
    spec = {
        key: descriptor[key]
        for key in ("transport", "command", "args", "url")
        if descriptor.get(key)
    }
    for key in ("auth", "headers", "env"):
        if previous.get(key):
            if previous.get("url") != spec.get("url") or previous.get("command") != spec.get(
                "command"
            ):
                raise ValueError("MCP endpoint changed; reconnect its credentials before Reload")
            spec[key] = previous[key]
    return spec


def _probe(spec: dict[str, Any]) -> list[Any]:
    from clio_agent.tools.mcp_runtime import make_mcp_client
    from clio_agent.tools.mcp_server_progress import wait_while_server_works

    async def listing() -> list[Any]:
        async with make_mcp_client(spec) as client:
            return list(await client.list_tools())

    return asyncio.run(wait_while_server_works(listing(), op_name="blueprint descriptor Reload"))


def prepare_enabled_descriptors(
    app: Any, staged: Path, destination: Path, *, scope: str, cwd: Path
) -> tuple[list[dict[str, Any]], dict[str, tuple[dict[str, Any], dict[str, Any] | None]]]:
    """Probe only descriptors previously enabled for this exact installed identity.

    The returned rows still point at the final installation path. They are not
    applied until the complete filesystem revision succeeds at the turn boundary.
    """
    from clio_agent.gact.agent_blueprints import discover_agent_blueprints
    from clio_agent.gact.agents.tool_instrumentation import mcp_tool_title
    from clio_agent.gact.permission_gate import _normalize_mcp_tool_annotations

    blueprint = parse_agent_blueprint_root(staged, scope=scope)
    identity = identity_fields(blueprint)["identity"]
    enabled = getattr(app.state, "external_mcp_servers", {})
    descriptors = {
        row["id"]: row for row in load_mcp_descriptors(staged, scope=scope, blueprint_id=identity)
    }
    checks: list[dict[str, Any]] = []
    updates: dict[str, tuple[dict[str, Any], dict[str, Any] | None]] = {}
    for server_id, previous in list(enabled.items()):
        if previous.get("source") != "agent_blueprint" or previous.get("status") == "disabled":
            continue
        previous_identity = previous.get("agent_blueprint_identity") or previous.get(
            "agent_blueprint_id"
        )
        if previous_identity == blueprint.id:
            # Legacy ID-only rows are accepted only when discovery resolves one
            # unambiguous installed owner. Never guess between marketplaces.
            owner = select_blueprint(discover_agent_blueprints(cwd=cwd), blueprint.id)
            if owner is None or owner.root.resolve() != destination.resolve():
                continue
        elif previous_identity != identity:
            continue
        descriptor_id = str(previous["descriptor_id"])
        descriptor = descriptors.get(descriptor_id)
        if descriptor is None:
            updates[server_id] = (deepcopy(previous), None)
            checks.append({"namespace": descriptor_id, "status": "removed_from_revision"})
            continue
        if descriptor.get("validation_errors"):
            raise ValueError(f"Enabled MCP descriptor {descriptor_id} is invalid")
        probe_spec = _spec(descriptor, previous.get("spec") or {})
        try:
            listed = _probe(probe_spec)
        except (
            OSError,
            ValueError,
            RuntimeError,
            ExceptionGroup,
            FastMCPError,
            MCPError,
            ClientError,
            HTTPError,
            BrokenResourceError,
            EndOfStream,
        ) as exc:
            raise ValueError(
                f"MCP descriptor {descriptor_id} could not initialize/list tools "
                f"({type(exc).__name__}); the previous revision was retained"
            ) from None
        names = {str(tool.name) for tool in listed}
        expected = {str(tool["name"]) for tool in descriptor.get("tools", [])}
        if expected - names:
            raise ValueError(
                f"MCP descriptor {descriptor_id} is missing declared tools after startup"
            )
        final_spec = _spec(descriptor, previous.get("spec") or {})
        for key, value in final_spec.items():
            if isinstance(value, str):
                final_spec[key] = value.replace(str(staged), str(destination))
            elif isinstance(value, list):
                final_spec[key] = [item.replace(str(staged), str(destination)) for item in value]
        row = {
            **previous,
            "agent_blueprint_identity": identity,
            "spec": final_spec,
            "status": "ready" if listed else "no_tools",
            "error": "",
            "transport": descriptor["transport"],
            "tools": [
                {
                    "id": tool.name,
                    "name": tool.name,
                    "description": tool.description or "",
                    "title": mcp_tool_title(tool) or "",
                    "annotations": _normalize_mcp_tool_annotations(tool),
                    "enabled": True,
                    "status": "ready",
                    "server_id": server_id,
                    "descriptor_id": descriptor_id,
                    "agent_blueprint_id": identity,
                    "input_schema": tool.inputSchema,
                    "output_schema": getattr(tool, "outputSchema", None) or {},
                }
                for tool in listed
            ],
        }
        updates[server_id] = (deepcopy(previous), row)
        checks.append({"namespace": descriptor_id, "status": "ready", "tool_count": len(listed)})
    return checks, updates
