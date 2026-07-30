"""Tests for the single MCP client factory and its handler shape (#1106).

``make_mcp_client`` is the ONE execution-path construction site for FastMCP
clients. It owns the typed handler SLOT (:class:`MCPClientHandlers` ->
``mcp_handlers`` adapters) — construction-time only; no hook is wired until P1's
correlation-by-protocol-identity work. Every execution path (executor default,
per-call dispatch, gateway proxy backend, dynamic-agent tool call, handshake
probe) routes through it. This suite covers handler attachment, mapping
disambiguation (incl. the colliding ``transport`` name), and the clone-safe
message multiplexer.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest

from clio_agent.tools import mcp_executor
from clio_agent.tools.mcp_executor import AsyncMCPToolExecutor
from clio_agent.tools.mcp_handlers import (
    ElicitationDispatcher,
    MessageMultiplexer,
    ProgressDispatcher,
)
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
        return SimpleNamespace(data="ok", content=[])

    async def read_resource(self, uri: str) -> Any:
        return None


# --------------------------------------------------------------------------- #
# Factory: handler attachment + zero-change bare construction
# --------------------------------------------------------------------------- #


def test_make_mcp_client_wraps_hooks_in_signature_adapters() -> None:
    """Each populated hook is wrapped in a signature adapter and forwarded."""

    async def elicit(context: Any, *a: Any) -> Any:
        return None

    async def progress(context: Any, *a: Any) -> None:
        return None

    handlers = MCPClientHandlers(elicitation=elicit, progress=progress)
    client = make_mcp_client("transport-sentinel", handlers=handlers, client_cls=_FakeClient)

    elicit_dispatcher = client.kwargs["elicitation_handler"]
    progress_dispatcher = client.kwargs["progress_handler"]
    assert isinstance(elicit_dispatcher, ElicitationDispatcher)
    assert elicit_dispatcher._hook is elicit
    assert isinstance(progress_dispatcher, ProgressDispatcher)
    assert progress_dispatcher._hook is progress


def test_make_mcp_client_message_hook_is_multiplexer() -> None:
    """A message hook becomes a MessageMultiplexer adapter."""

    async def on_message(context: Any, message: Any) -> None:
        return None

    handlers = MCPClientHandlers(message=on_message)
    client = make_mcp_client("t", handlers=handlers, client_cls=_FakeClient)

    mux = client.kwargs["message_handler"]
    assert isinstance(mux, MessageMultiplexer)
    assert mux._hook is on_message


def test_make_mcp_client_no_handlers_stamps_identity_only() -> None:
    """No handlers => identity-only construction (client_info, no handler kwargs)."""

    client = make_mcp_client("transport-sentinel", client_cls=_FakeClient)

    assert client.target == "transport-sentinel"
    assert set(client.kwargs) == {"client_info"}
    assert client.kwargs["client_info"].name == "clio-agent"


def test_make_mcp_client_all_none_hooks_stamps_identity_only() -> None:
    """An empty bundle (all hooks None) still yields an identity-only client."""

    client = make_mcp_client("t", handlers=MCPClientHandlers(), client_cls=_FakeClient)

    assert set(client.kwargs) == {"client_info"}
    assert client.kwargs["client_info"].name == "clio-agent"


# --------------------------------------------------------------------------- #
# Finding #3: mapping target disambiguation
# --------------------------------------------------------------------------- #


def test_mapping_with_transport_key_goes_through_transport_from_spec(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A CLIO raw spec (has 'transport') is resolved via transport_from_spec."""

    seen: dict[str, Any] = {}

    def fake_transport_from_spec(spec: Any) -> Any:
        seen["spec"] = spec
        return "resolved-transport"

    monkeypatch.setattr("clio_agent.tools.mcp_config.transport_from_spec", fake_transport_from_spec)
    spec = {"transport": "stdio", "command": "echo"}
    client = make_mcp_client(spec, client_cls=_FakeClient)

    assert seen["spec"] == spec
    assert client.target == "resolved-transport"


def test_mapping_mcpconfig_wrapper_is_passed_unchanged(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A native FastMCP MCPConfig ('mcpServers') is handed to Client unchanged."""

    def boom(spec: Any) -> Any:  # transport_from_spec must NOT be called
        raise AssertionError("MCPConfig must not go through transport_from_spec")

    monkeypatch.setattr("clio_agent.tools.mcp_config.transport_from_spec", boom)
    config = {"mcpServers": {"remote": {"url": "https://h/mcp"}}}
    client = make_mcp_client(config, client_cls=_FakeClient)

    assert client.target is config


def test_mapping_rootless_mcpconfig_is_passed_unchanged(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A rootless MCPConfig (server map at root) is handed to Client unchanged."""

    def boom(spec: Any) -> Any:
        raise AssertionError("rootless MCPConfig must not go through transport_from_spec")

    monkeypatch.setattr("clio_agent.tools.mcp_config.transport_from_spec", boom)
    config = {"remote": {"url": "https://h/mcp"}, "local": {"command": "x"}}
    client = make_mcp_client(config, client_cls=_FakeClient)

    assert client.target is config


def test_mapping_transport_named_server_is_mcpconfig_not_clio_spec() -> None:
    """A rootless MCPConfig server merely NAMED 'transport' is not a CLIO spec.

    Built against the real fastmcp Client: the colliding-name mapping must reach
    the Client as an MCPConfig (MCPConfigTransport), never be mis-parsed by
    transport_from_spec as a stdio spec whose transport kind is a dict.
    """

    from fastmcp import Client
    from fastmcp.client.transports import MCPConfigTransport

    # top-level key "transport" whose VALUE is a server config mapping
    target = {"transport": {"command": "x"}}
    client = make_mcp_client(target)

    assert isinstance(client, Client)
    assert isinstance(client.transport, MCPConfigTransport)


def test_mapping_ambiguous_raises_value_error() -> None:
    """A mapping that is neither a CLIO spec nor an MCPConfig is a hard error."""

    with pytest.raises(ValueError, match="ambiguous MCP client target mapping"):
        make_mcp_client({"nonsense": 1, "keys": 2}, client_cls=_FakeClient)


# --------------------------------------------------------------------------- #
# FastMCP 4 message-hook cloning
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_message_multiplexer_forwards_hook() -> None:
    """The message adapter forwards protocol messages to the CLIO hook."""
    seen: list[str] = []

    async def hook(context: Any, message: Any) -> None:
        seen.append(f"hook:{message}")

    mux = MessageMultiplexer(hook)
    await mux("PING")

    assert seen == ["hook:PING"]


@pytest.mark.asyncio
async def test_message_multiplexer_survives_client_clone() -> None:
    """FastMCP 4 ``Client.new`` preserves the message adapter on a clone."""
    from fastmcp import FastMCP

    fired: list[Any] = []

    async def hook(context: Any, message: Any) -> None:
        fired.append(message)

    client = make_mcp_client(FastMCP("backend"), handlers=MCPClientHandlers(message=hook))
    clone = client.new()

    original_mux = client._session_kwargs["message_handler"]
    clone_mux = clone._session_kwargs["message_handler"]
    assert isinstance(clone_mux, MessageMultiplexer)
    assert clone_mux is original_mux

    await clone_mux("PING")

    assert fired == ["PING"]


# --------------------------------------------------------------------------- #
# Executor routing: default + per-call dispatch through the factory
# --------------------------------------------------------------------------- #


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
    assert executor._client_factory is spy_factory

    await executor.start()  # composite client built via the factory (server)
    await executor._route("ns_tool")  # per-call dispatch builds the ns client (proxy)

    assert calls == [server, proxy]


# --------------------------------------------------------------------------- #
# Finding #1: gateway proxy backend routes through the factory + reachability
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_proxy_backend_carries_factory_handler_onto_upstream() -> None:
    """A handler on the factory-built backend survives the proxy's per-request clone."""

    from fastmcp import FastMCP
    from fastmcp.server import create_proxy

    backend = FastMCP("backend")

    @backend.tool
    def ping() -> str:
        return "pong"

    async def elicit(context: Any, *a: Any) -> Any:
        return None

    backend_client = make_mcp_client(backend, handlers=MCPClientHandlers(elicitation=elicit))
    installed_cb = backend_client._session_kwargs["elicitation_callback"]

    proxy = create_proxy(backend_client)
    # The proxy runs client.new() per request; copy.copy carries the handler
    # onto that upstream client, so the callback reaches the real call path.
    upstream = proxy.client_factory()
    assert upstream._session_kwargs.get("elicitation_callback") is installed_cb


def test_proxy_for_spec_routes_backend_through_factory(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """_proxy_for_spec builds its backend client via make_mcp_client (with handlers).

    Finding #3: the handler-aware backend is now built by a PER-REQUEST
    client_factory (so each backend leg mirrors the front request's negotiated
    protocol era). The factory still routes through ``make_mcp_client`` with the
    exact ``(transport, handlers)`` — invoked when the proxy asks for a client.
    """

    from fastmcp import Client, FastMCP

    from clio_agent.tools import gateway
    from clio_agent.tools.mcp_config import MCPServerSpec

    async def elicit(context: Any, *a: Any) -> Any:
        return None

    handlers = MCPClientHandlers(elicitation=elicit)
    calls: list[Any] = []
    stub = FastMCP("stub")

    def spy(target: Any, *, handlers: Any = None) -> Any:  # noqa: A002 - shadows param name intentionally
        calls.append((target, handlers))
        return Client(stub)  # a real client so the proxy accepts it

    monkeypatch.setattr(gateway, "transport_for", lambda spec, cwd=None: "TSPORT")
    # gateway imports make_mcp_client function-locally; patch it at the source.
    monkeypatch.setattr("clio_agent.tools.mcp_runtime.make_mcp_client", spy)

    proxy = gateway._proxy_for_spec(
        MCPServerSpec(name="ext", transport="stdio", command="x"), handlers=handlers
    )
    # No eager construction: the transport is bound but no backend client is built
    # until the proxy dispatches a request.
    assert calls == []

    backend = proxy.client_factory()  # one per-request build
    assert calls == [("TSPORT", handlers)]
    assert backend is not None


@pytest.mark.asyncio
async def test_external_server_through_gateway_to_executor(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """End-to-end: external server -> proxy -> executor, with handlers threaded."""

    from fastmcp import FastMCP

    from clio_agent.tools import gateway
    from clio_agent.tools.gateway import build_gateway, namespace_proxies
    from clio_agent.tools.mcp_config import MCPServerSpec

    backend = FastMCP("backend")

    @backend.tool
    def ping() -> str:
        return "pong"

    async def elicit(context: Any, *a: Any) -> Any:
        return None

    monkeypatch.setattr(gateway, "transport_for", lambda spec, cwd=None: backend)
    handlers = MCPClientHandlers(elicitation=elicit)
    gw = build_gateway(
        {"ext": MCPServerSpec(name="ext", transport="stdio", command="x")},
        handlers=handlers,
    )

    # The mounted proxy's upstream client carries the handler (reachable upstream).
    proxy = namespace_proxies(gw)["ext"]
    upstream = proxy.client_factory()
    assert upstream._session_kwargs.get("elicitation_callback") is not None

    # ...and the wired path actually dispatches the backend tool.
    async with AsyncMCPToolExecutor(gw) as executor:
        out = await executor.call_tool("ext_ping", {})
    assert "pong" in out
