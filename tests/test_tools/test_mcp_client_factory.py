"""Tests for the single MCP client factory (#1106).

``make_mcp_client`` is the ONE execution-path construction site for FastMCP
clients. It owns the handler slot (``MCPClientHandlers``) that P1 fills with
elicitation/progress/message/cancellation handlers, and every execution path
(the executor default plus the per-call dispatch paths) routes through it.
"""

from __future__ import annotations

from typing import Any

import pytest

from clio_agent.tools import mcp_executor
from clio_agent.tools.mcp_executor import AsyncMCPToolExecutor
from clio_agent.tools.mcp_runtime import MCPClientHandlers, make_mcp_client


class _FakeClient:
    """Records the transport target and handler kwargs it was built with."""

    def __init__(self, target: Any, **kwargs: Any) -> None:
        self.target = target
        self.kwargs = kwargs

    async def __aenter__(self) -> "_FakeClient":
        return self

    async def __aexit__(self, exc_type: Any, exc: Any, tb: Any) -> None:
        return None

    async def list_tools(self) -> list[Any]:
        return []

    async def call_tool(self, name: str, arguments: dict[str, Any]) -> Any:
        return None

    async def read_resource(self, uri: str) -> Any:
        return None


def test_make_mcp_client_attaches_handler_bundle() -> None:
    """A handler bundle passed to the factory is attached to the client."""

    elicitation = object()
    progress = object()
    message = object()
    handlers = MCPClientHandlers(
        elicitation=elicitation,
        progress=progress,
        message=message,
    )

    client = make_mcp_client("transport-sentinel", handlers=handlers, client_cls=_FakeClient)

    assert isinstance(client, _FakeClient)
    assert client.target == "transport-sentinel"
    assert client.kwargs["elicitation_handler"] is elicitation
    assert client.kwargs["progress_handler"] is progress
    assert client.kwargs["message_handler"] is message


def test_make_mcp_client_no_handlers_is_bare_construction() -> None:
    """No handlers => identical bare construction (zero behavior change)."""

    client = make_mcp_client("transport-sentinel", client_cls=_FakeClient)

    assert isinstance(client, _FakeClient)
    assert client.target == "transport-sentinel"
    assert client.kwargs == {}


def test_make_mcp_client_maps_spec_mapping_through_transport_from_spec(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A Mapping target is resolved via mcp_config.transport_from_spec."""

    seen: dict[str, Any] = {}

    def fake_transport_from_spec(spec: Any) -> Any:
        seen["spec"] = spec
        return "resolved-transport"

    monkeypatch.setattr(
        "clio_agent.tools.mcp_config.transport_from_spec",
        fake_transport_from_spec,
    )

    spec = {"transport": "stdio", "command": "echo"}
    client = make_mcp_client(spec, client_cls=_FakeClient)

    assert seen["spec"] == spec
    assert client.target == "resolved-transport"


def test_executor_default_factory_is_make_mcp_client() -> None:
    """The executor's default client_factory routes through the factory."""

    executor = AsyncMCPToolExecutor(object())

    assert executor._client_factory is make_mcp_client


@pytest.mark.asyncio
async def test_executor_dispatch_paths_route_through_make_mcp_client(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Default construction AND per-call dispatch both call the factory."""

    calls: list[Any] = []

    def spy_factory(target: Any) -> _FakeClient:
        calls.append(target)
        return _FakeClient(target)

    monkeypatch.setattr(mcp_executor, "make_mcp_client", spy_factory)

    server = object()
    proxy = object()
    executor = AsyncMCPToolExecutor(
        server,
        preloaded_tools={"ns_tool": object()},
        namespace_servers={"ns": proxy},
    )

    # Default construction routes through the (spied) factory.
    assert executor._client_factory is spy_factory

    await executor.start()  # composite client built via the factory (server)
    await executor._route("ns_tool")  # per-call dispatch builds the namespace client (proxy)

    assert calls == [server, proxy]
