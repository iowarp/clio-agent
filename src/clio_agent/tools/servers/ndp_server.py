"""National Data Platform tools backed by clio-kit MCP."""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

from fastmcp import Client, FastMCP
from fastmcp.client.transports import StdioTransport

ndp_server = FastMCP("ndp")


def _clio_kit_transport(server_name: str = "ndp") -> StdioTransport:
    """Return a stdio transport for a local clio-kit checkout or uvx package."""
    configured = os.environ.get("CLIO_KIT_PATH", "").strip()
    local_path = Path(configured).expanduser() if configured else Path("../clio-kit")
    local_path = local_path.resolve()
    if local_path.exists():
        return StdioTransport(
            command="uv",
            args=[
                "--directory",
                str(local_path),
                "run",
                "clio-kit",
                "mcp-server",
                server_name,
            ],
        )
    return StdioTransport(command="uvx", args=["clio-kit", "mcp-server", server_name])


def _decode_text_content(result: Any) -> dict[str, Any]:
    """Decode a clio-kit FastMCP tool result into a plain dictionary."""
    data = getattr(result, "data", None)
    if isinstance(data, dict):
        return data
    structured = getattr(result, "structured_content", None)
    if isinstance(structured, dict):
        return structured
    for part in getattr(result, "content", []) or []:
        text = getattr(part, "text", "")
        if not text:
            continue
        try:
            decoded = json.loads(text)
        except json.JSONDecodeError:
            return {"text": text}
        if isinstance(decoded, dict):
            return decoded
        return {"data": decoded}
    return {}


def _tool_error(
    *,
    code: str,
    message: str,
    next_action: str,
    details: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Return CLIO's structured tool error shape."""
    error: dict[str, Any] = {
        "type": "tool_error",
        "code": code,
        "message": message,
        "next_action": next_action,
    }
    if details:
        error["details"] = details
    return {"error": error}


def _clean_optional_text(value: str | None) -> str | None:
    """Normalize planner-produced empty strings to absent optional values."""
    if value is None:
        return None
    cleaned = str(value).strip()
    return cleaned or None


def _clean_server(value: str | None, *, allowed: set[str]) -> str:
    """Normalize NDP server selector while preserving explicit valid choices."""
    cleaned = _clean_optional_text(value)
    if cleaned in allowed:
        return cleaned
    return "global"


def _clean_string_list(value: list[str] | None) -> list[str] | None:
    """Remove empty planner placeholders from optional string lists."""
    if not value:
        return None
    cleaned = [str(item).strip() for item in value if str(item).strip()]
    return cleaned or None


def _clean_limit(value: int | str | None) -> int | None:
    """Normalize optional result limit from planner/tool arguments."""
    if value is None or value == "":
        return None
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return None
    return max(1, min(parsed, 20))


def _compact_resource_formats(resources: Any) -> tuple[list[str], int, list[str]]:
    """Return compact resource format/name summaries for an NDP dataset row."""
    if not isinstance(resources, list):
        return [], 0, []
    formats = sorted(
        {
            str(resource.get("format")).upper()
            for resource in resources
            if isinstance(resource, dict) and resource.get("format")
        }
    )
    names = [
        str(resource.get("name") or resource.get("url") or "").strip()
        for resource in resources
        if isinstance(resource, dict) and (resource.get("name") or resource.get("url"))
    ]
    return formats[:5], len(resources), names[:1]


def _compact_dataset(row: Any) -> Any:
    """Keep catalog rows useful for LLM synthesis without flooding context."""
    if not isinstance(row, dict):
        return row
    formats, resource_count, resource_names = _compact_resource_formats(row.get("resources"))
    notes = str(row.get("notes") or "").strip()
    if len(notes) > 120:
        notes = notes[:117] + "..."
    return {
        "id": row.get("id"),
        "name": row.get("name"),
        "title": row.get("title"),
        "owner_org": row.get("owner_org"),
        "notes": notes,
        "resource_count": resource_count,
        "resource_formats": formats,
        "resource_names": resource_names,
    }


def _compact_ndp_result(tool_name: str, decoded: dict[str, Any]) -> dict[str, Any]:
    """Compact successful clio-kit NDP payloads before they enter CLIO traces."""
    compacted = dict(decoded)
    if tool_name == "list_organizations":
        organizations = compacted.get("organizations")
        if isinstance(organizations, list) and len(organizations) > 8:
            compacted["organizations"] = organizations[:8]
            compacted["organizations_truncated"] = True
        return compacted
    if tool_name == "search_datasets":
        compacted.pop("search_parameters", None)
        datasets = compacted.get("datasets")
        if isinstance(datasets, list):
            compacted["datasets"] = [_compact_dataset(row) for row in datasets[:4]]
            compacted["datasets_truncated"] = len(datasets) > 4
        return compacted
    if tool_name == "get_dataset_details":
        return _compact_dataset(compacted)
    return compacted


async def _call_clio_kit_ndp_tool(tool_name: str, args: dict[str, Any]) -> dict[str, Any]:
    """Call one clio-kit NDP MCP tool and surface upstream errors explicitly."""
    try:
        async with Client(_clio_kit_transport("ndp")) as client:
            result = await client.call_tool(tool_name, args)
    except Exception as exc:
        return _tool_error(
            code="clio_kit_ndp_unavailable",
            message=f"Could not call clio-kit NDP MCP tool {tool_name!r}: {exc}",
            next_action=(
                "Install clio-kit or set CLIO_KIT_PATH to a local checkout, then retry."
            ),
            details={"tool": tool_name, "args": args},
        )

    if bool(getattr(result, "is_error", False)):
        return _tool_error(
            code="clio_kit_ndp_error",
            message=f"clio-kit NDP MCP tool {tool_name!r} returned an error.",
            next_action="Inspect the returned MCP content and retry with corrected arguments.",
            details={"tool": tool_name, "content": _decode_text_content(result)},
        )

    decoded = _decode_text_content(result)
    if not decoded:
        return _tool_error(
            code="empty_tool_result",
            message=f"clio-kit NDP MCP tool {tool_name!r} returned no structured content.",
            next_action="Retry the call or inspect clio-kit NDP server logs.",
            details={"tool": tool_name},
        )
    return _compact_ndp_result(tool_name, decoded)


@ndp_server.tool()
async def list_organizations(
    name_filter: str | None = None,
    server: str = "global",
) -> dict[str, Any]:
    """List organizations available in the National Data Platform."""
    args: dict[str, Any] = {
        "name_filter": _clean_optional_text(name_filter),
        "server": _clean_server(server, allowed={"local", "global", "pre_ckan"}),
    }
    return await _call_clio_kit_ndp_tool("list_organizations", args)


@ndp_server.tool()
async def search_datasets(
    search_terms: list[str] | None = None,
    search_keys: list[str] | None = None,
    dataset_name: str | None = None,
    dataset_title: str | None = None,
    owner_org: str | None = None,
    resource_url: str | None = None,
    resource_name: str | None = None,
    dataset_description: str | None = None,
    resource_description: str | None = None,
    resource_format: str | None = None,
    search_term: str | None = None,
    filter_list: list[str] | None = None,
    timestamp: str | None = None,
    server: str = "global",
    limit: int | str | None = None,
) -> dict[str, Any]:
    """Search for datasets in the NDP using terms or field-specific criteria."""
    args: dict[str, Any] = {
        "search_terms": _clean_string_list(search_terms),
        "search_keys": _clean_string_list(search_keys),
        "dataset_name": _clean_optional_text(dataset_name),
        "dataset_title": _clean_optional_text(dataset_title),
        "owner_org": _clean_optional_text(owner_org),
        "resource_url": _clean_optional_text(resource_url),
        "resource_name": _clean_optional_text(resource_name),
        "dataset_description": _clean_optional_text(dataset_description),
        "resource_description": _clean_optional_text(resource_description),
        "resource_format": _clean_optional_text(resource_format),
        "search_term": _clean_optional_text(search_term),
        "filter_list": _clean_string_list(filter_list),
        "timestamp": _clean_optional_text(timestamp),
        "server": _clean_server(server, allowed={"local", "global"}),
        "limit": _clean_limit(limit),
    }
    return await _call_clio_kit_ndp_tool(
        "search_datasets",
        {key: value for key, value in args.items() if value is not None},
    )


@ndp_server.tool()
async def get_dataset_details(
    dataset_identifier: str,
    identifier_type: str = "id",
    server: str = "global",
) -> dict[str, Any]:
    """Retrieve detailed metadata for a specific NDP dataset by ID or name."""
    args = {
        "dataset_identifier": dataset_identifier,
        "identifier_type": identifier_type,
        "server": server,
    }
    return await _call_clio_kit_ndp_tool("get_dataset_details", args)
