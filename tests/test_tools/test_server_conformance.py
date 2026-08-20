"""MCP 2026-07-28 conformance pins for CLIO's own FastMCP servers."""

from __future__ import annotations

from collections.abc import Callable, Mapping
from pathlib import Path
from typing import Any

import pytest
from fastmcp import Client, FastMCP

from clio_agent.tools.gateway import build_gateway
from clio_agent.tools.servers.fs_server import fs_server
from clio_agent.tools.servers.shell_server import shell_server

PROTOCOL_VERSION = "2026-07-28"
TASKS_EXTENSION = "io.modelcontextprotocol/tasks"

FS_ANNOTATIONS: dict[str, dict[str, bool]] = {
    "read_file": {"readOnlyHint": True, "openWorldHint": False},
    "propose_edit": {"readOnlyHint": True, "openWorldHint": False},
    "apply_edit_write": {
        "readOnlyHint": False,
        "destructiveHint": True,
        "openWorldHint": False,
    },
}
SHELL_ANNOTATIONS: dict[str, dict[str, bool]] = {
    "bash": {
        "readOnlyHint": False,
        "destructiveHint": True,
        "openWorldHint": True,
    }
}
GATEWAY_ANNOTATIONS: dict[str, dict[str, bool]] = {
    **{f"fs_{name}": hints for name, hints in FS_ANNOTATIONS.items()},
    **{f"shell_{name}": hints for name, hints in SHELL_ANNOTATIONS.items()},
}


def _gateway() -> FastMCP:
    """Build a fresh built-ins-only gateway for an isolated client session."""

    return build_gateway({})


SERVER_CASES: tuple[tuple[str, Callable[[], FastMCP], Mapping[str, Mapping[str, bool]]], ...] = (
    ("fs", lambda: fs_server, FS_ANNOTATIONS),
    ("shell", lambda: shell_server, SHELL_ANNOTATIONS),
    ("gateway", _gateway, GATEWAY_ANNOTATIONS),
)


@pytest.mark.asyncio
@pytest.mark.parametrize(("name", "server_factory", "_annotations"), SERVER_CASES)
async def test_servers_negotiate_2026_07_28_and_answer_discover(
    name: str,
    server_factory: Callable[[], FastMCP],
    _annotations: Mapping[str, Mapping[str, bool]],
) -> None:
    """Each server negotiates the modern era and exposes discovery metadata."""

    async with Client(server_factory(), cache=False) as client:
        assert client.protocol_version == PROTOCOL_VERSION, name
        assert client.server_info is not None, name
        assert client.server_info.name, name
        assert client.server_capabilities is not None, name
        assert client.server_capabilities.tools is not None, name


@pytest.mark.asyncio
@pytest.mark.parametrize(("name", "server_factory", "expected_annotations"), SERVER_CASES)
async def test_declared_tools_expose_annotations_and_snake_case_input_schema(
    name: str,
    server_factory: Callable[[], FastMCP],
    expected_annotations: Mapping[str, Mapping[str, bool]],
) -> None:
    """Every declared tool retains annotations and ``input_schema``."""

    async with Client(server_factory(), cache=False) as client:
        tools = {tool.name: tool for tool in await client.list_tools()}

    assert set(tools) == set(expected_annotations), name
    for tool_name, expected_hints in expected_annotations.items():
        tool = tools[tool_name]
        assert tool.input_schema, tool_name
        assert tool.input_schema["type"] == "object", tool_name
        assert tool.annotations is not None, tool_name
        assert tool.annotations.model_dump(by_alias=True, exclude_none=True) == expected_hints


@pytest.mark.asyncio
@pytest.mark.parametrize(("name", "server_factory", "_annotations"), SERVER_CASES)
async def test_servers_keep_tasks_off(
    name: str,
    server_factory: Callable[[], FastMCP],
    _annotations: Mapping[str, Mapping[str, bool]],
) -> None:
    """CLIO servers leave durable tasks to the relay and do not advertise them."""

    async with Client(server_factory(), cache=False) as client:
        capabilities = client.server_capabilities
        assert capabilities is not None, name
        assert capabilities.tasks is None, name
        assert TASKS_EXTENSION not in (capabilities.extensions or {}), name
        advertised = capabilities.model_dump(mode="json", by_alias=True, exclude_none=True)
        assert TASKS_EXTENSION not in str(advertised), name


@pytest.mark.asyncio
async def test_structured_tool_results_use_modern_envelopes(tmp_path: Path) -> None:
    """Structured calls expose both content views and complete synchronously."""

    sample = tmp_path / "sample.txt"
    sample.write_text("hello", encoding="utf-8")
    calls: tuple[tuple[FastMCP, str, dict[str, Any]], ...] = (
        (fs_server, "read_file", {"filepath": str(sample)}),
        (shell_server, "bash", {"command": ""}),
        (_gateway(), "fs_read_file", {"filepath": str(sample)}),
    )

    for server, tool_name, arguments in calls:
        async with Client(server, cache=False) as client:
            result = await client.call_tool(tool_name, arguments)

        assert result.is_error is False, tool_name
        assert result.content, tool_name
        assert result.structured_content is not None, tool_name
        assert result.data == result.structured_content, tool_name


@pytest.mark.asyncio
async def test_legacy_client_round_trips_gateway_tool(tmp_path: Path) -> None:
    """The modern gateway still accepts the legacy initialize handshake."""

    sample = tmp_path / "legacy.txt"
    sample.write_text("legacy", encoding="utf-8")

    async with Client(_gateway(), mode="legacy", cache=False) as client:
        result = await client.call_tool("fs_read_file", {"filepath": str(sample)})

        assert client.protocol_version == "2025-11-25"
        assert result.data["content"] == "legacy"
        assert result.is_error is False
