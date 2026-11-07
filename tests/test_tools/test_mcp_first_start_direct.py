"""A server's first start connects directly (B3).

Its capability is unknown until it has been listed once. Connecting the first start
through the proxy and then healing to a direct route started the server twice; the
first start connects directly instead, and reads the real capability from it.
"""

from __future__ import annotations

from clio_agent.errors import MCP_TASK_CAPABILITY_UNKNOWN, MCP_TASKS_DIRECT_ROUTE_SELECTED
from clio_agent.tools.mcp_connection_era import record_task_capability
from clio_agent.tools.mcp_task_routing import (
    MCP_PREPARE_FIRST_START_DIRECT,
    first_start_direct,
    resolve_and_build_direct_client,
)


def test_an_unknown_server_connects_through_the_proxy_outside_a_first_start() -> None:
    namespace = "unit-first-start-outside"
    client, decision = resolve_and_build_direct_client(namespace, {namespace: object})
    assert client is None
    assert decision.reason == MCP_TASK_CAPABILITY_UNKNOWN


def test_a_first_start_of_an_unknown_server_connects_directly() -> None:
    namespace = "unit-first-start-direct"
    built = object()
    with first_start_direct(namespace):
        client, decision = resolve_and_build_direct_client(namespace, {namespace: lambda: built})
    assert client is built
    assert decision.use_direct is True
    assert decision.reason == MCP_PREPARE_FIRST_START_DIRECT


def test_a_first_start_without_a_direct_factory_keeps_the_proxy() -> None:
    namespace = "unit-first-start-no-factory"
    with first_start_direct(namespace):
        client, decision = resolve_and_build_direct_client(namespace, {})
    assert client is None
    assert decision.reason == MCP_TASK_CAPABILITY_UNKNOWN


def test_a_known_verdict_wins_over_the_first_start_rule() -> None:
    namespace = "unit-first-start-known"
    record_task_capability(namespace, task_capable=True, source="capabilities_extensions")
    built = object()
    with first_start_direct("another-namespace"):
        client, decision = resolve_and_build_direct_client(namespace, {namespace: lambda: built})
    assert client is built
    assert decision.reason == MCP_TASKS_DIRECT_ROUTE_SELECTED


NOTES_SERVER = """
from fastmcp import FastMCP

mcp = FastMCP("notes")
NOTE = {"text": "empty"}


@mcp.tool
def set_note(text: str) -> str:
    NOTE["text"] = text
    return "saved"


@mcp.prompt
def current_note() -> str:
    return "note: " + NOTE["text"]


@mcp.resource("note://current")
def note() -> str:
    return NOTE["text"]


mcp.run()
"""


async def test_a_plain_servers_prompts_and_resources_see_what_its_tool_calls_changed(
    tmp_path,
) -> None:
    """DS34: a plain server's tool calls and its prompts reach the same process.

    Product tool calls connect to the namespace's proxy (``ExternalTasks``), whose
    kept-alive backend is one process. The first start connected directly to a second
    process, so prompts and resources never saw what a call changed.
    """

    import sys

    import psutil

    from clio_agent.tools.gateway import build_gateway, namespace_proxies, namespace_specs
    from clio_agent.tools.mcp_config import MCPServerSpec
    from clio_agent.tools.mcp_executor import AsyncMCPToolExecutor

    namespace = "ds34notes"
    script = tmp_path / "ds34_notes_server.py"
    script.write_text(NOTES_SERVER, encoding="utf-8")
    spec = MCPServerSpec(
        name=namespace, transport="stdio", command=sys.executable, args=(str(script),)
    )
    gateway = build_gateway({namespace: spec})
    executor = AsyncMCPToolExecutor(
        gateway, namespace_servers=namespace_proxies(gateway), preloaded_tools={}
    )
    executor._clio_namespace_specs = namespace_specs(gateway)  # noqa: SLF001
    proxy = executor._namespace_servers[namespace]  # noqa: SLF001
    try:
        async with executor:
            listed = await executor.list_namespace_tools(namespace)
            assert [tool.name for tool in listed] == ["set_note"]
            assert not executor.is_namespace_prepared(namespace)

            # A product call: its own connection to the proxy, as ExternalTasks opens it.
            async with executor._client_factory(proxy) as calls:  # noqa: SLF001
                await calls.call_tool("set_note", {"text": "the spill folder"})

            prompt = await executor.get_namespace_prompt(namespace, "current_note", {})
            assert prompt.messages[0].content.text == "note: the spill folder"
            client = await executor._namespace_client(namespace, proxy)  # noqa: SLF001
            resource = await client.read_resource("note://current")
            assert resource[0].text == "the spill folder"
            assert executor._namespace_direct_routes[namespace] is False  # noqa: SLF001
    finally:
        for process in psutil.Process().children(recursive=True):
            try:
                if any(script.name in arg for arg in process.cmdline()):
                    process.kill()
            except psutil.Error:
                continue
