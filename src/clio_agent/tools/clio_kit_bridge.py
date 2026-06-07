"""Shared bridge for calling clio-kit MCP servers from CLIO tool servers.

CLIO keeps heavyweight or domain-specific implementations in the separate
``clio-kit`` package, each as its own MCP server (``ndp``, ``geo``, ...). CLIO
tool servers proxy to them over stdio so CLIO core never takes on those
servers' dependencies. This module centralizes how that subprocess is located
and how a single tool call is made and decoded.
"""

from __future__ import annotations

import json
import os
import shlex
import shutil
from pathlib import Path
from typing import Any

from fastmcp import Client
from fastmcp.client.transports import StdioTransport


def clio_kit_transport(server_name: str) -> StdioTransport:
    """Return a stdio transport for a clio-kit MCP server.

    Resolution order: a local checkout (``CLIO_KIT_PATH`` or ``../clio-kit``),
    an explicit ``CLIO_KIT_COMMAND``, a ``clio-kit`` on ``PATH``, then ``uvx``.

    Args:
        server_name: clio-kit server to launch (e.g. ``"geo"``, ``"ndp"``).
    """
    configured = os.environ.get("CLIO_KIT_PATH", "").strip()
    local_path = Path(configured).expanduser() if configured else Path("../clio-kit")
    local_path = local_path.resolve()
    if local_path.exists():
        return StdioTransport(
            command="uv",
            args=["--directory", str(local_path), "run", "clio-kit", "mcp-server", server_name],
        )

    configured_command = os.environ.get("CLIO_KIT_COMMAND", "").strip()
    if configured_command:
        parts = shlex.split(configured_command)
        if parts:
            return StdioTransport(command=parts[0], args=[*parts[1:], "mcp-server", server_name])

    path_command = shutil.which("clio-kit")
    if path_command:
        return StdioTransport(command=path_command, args=["mcp-server", server_name])

    return StdioTransport(
        command="uvx",
        args=["--from", "clio-kit", "clio-kit", "mcp-server", server_name],
    )


def clio_kit_launcher_source() -> str:
    """Return how clio-kit would be launched, without starting it.

    Returns one of ``local_path``, ``explicit_command``, ``path_command``,
    ``uvx``, or ``""`` when no launcher is available.
    """
    configured = os.environ.get("CLIO_KIT_PATH", "").strip()
    local_path = Path(configured).expanduser() if configured else Path("../clio-kit")
    if local_path.resolve().exists():
        return "local_path"
    if os.environ.get("CLIO_KIT_COMMAND", "").strip():
        return "explicit_command"
    if shutil.which("clio-kit"):
        return "path_command"
    if os.environ.get("CLIO_KIT_ALLOW_UVX", "").strip().lower() in {"1", "true", "yes", "on"}:
        return "uvx"
    return ""


def decode_tool_result(result: Any) -> dict[str, Any]:
    """Decode a FastMCP tool result into a plain dictionary."""
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
        return decoded if isinstance(decoded, dict) else {"data": decoded}
    return {}


async def call_clio_kit_tool(server_name: str, tool_name: str, args: dict[str, Any]) -> dict[str, Any]:
    """Call one clio-kit MCP tool and surface upstream failures explicitly.

    Args:
        server_name: clio-kit server (e.g. ``"geo"``).
        tool_name: Tool to invoke on that server.
        args: Tool arguments.

    Returns:
        The decoded tool result, or an ``error`` dict if the server could not be
        launched or the tool reported an error.
    """
    try:
        async with Client(clio_kit_transport(server_name)) as client:
            result = await client.call_tool(tool_name, args)
    except Exception as exc:  # noqa: BLE001 - subprocess/transport failures become tool errors
        return {
            "error": f"Could not call clio-kit {server_name} tool {tool_name!r}: {exc}",
            "code": "clio_kit_unavailable",
            "next_action": (
                "Install clio-kit, set CLIO_KIT_PATH to a local checkout, set "
                "CLIO_KIT_COMMAND to a launcher, or ensure uvx can resolve clio-kit."
            ),
            "tool": tool_name,
        }

    if bool(getattr(result, "is_error", False)):
        return {
            "error": f"clio-kit {server_name} tool {tool_name!r} returned an error.",
            "code": "clio_kit_error",
            "details": decode_tool_result(result),
            "tool": tool_name,
        }
    return decode_tool_result(result)
