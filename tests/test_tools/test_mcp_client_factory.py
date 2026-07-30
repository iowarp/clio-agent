"""Tests for the single MCP client factory and its handler shape (#1106).

``make_mcp_client`` is the ONE execution-path construction site for FastMCP
clients. It owns the typed handler slot (:class:`MCPClientHandlers` ->
``mcp_handlers`` dispatchers) that P1 fills, and every execution path (executor
default, per-call dispatch, gateway proxy backend, dynamic-agent tool call,
handshake probe) routes through it. This suite also covers the review round:
gateway-proxy reachability, mapping disambiguation, the message multiplexer, and
the cross-session correlation seam.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest

from clio_agent.tools import mcp_executor
from clio_agent.tools.mcp_executor import AsyncMCPToolExecutor
from clio_agent.tools.mcp_handlers import (
    DEFAULT_CORRELATION_REGISTRY,
    ElicitationDispatcher,
    MCPCorrelationRegistry,
    MCPInvocationContext,
    MessageMultiplexer,
    ProgressDispatcher,
    current_invocation,
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


def test_make_mcp_client_wraps_hooks_in_correlated_dispatchers() -> None:
    """Each populated hook is wrapped in a dispatcher and forwarded to the client."""

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
    # Correlation seam stamped so the executor can bind per-call context.
    assert isinstance(client._clio_correlation_key, str)
    assert client._clio_correlation_registry is DEFAULT_CORRELATION_REGISTRY


def test_make_mcp_client_message_hook_is_multiplexer() -> None:
    """A message hook becomes a MessageMultiplexer with a bound task handler."""

    async def on_message(context: Any, message: Any) -> None:
        return None

    handlers = MCPClientHandlers(message=on_message)
    client = make_mcp_client("t", handlers=handlers, client_cls=_FakeClient)

    mux = client.kwargs["message_handler"]
    assert isinstance(mux, MessageMultiplexer)
    assert mux._hook is on_message
    # Built-in task-status routing preserved: a TaskNotificationHandler is bound.
    assert mux._task_handler is not None


def test_make_mcp_client_no_handlers_is_bare_construction() -> None:
    """No handlers => identical bare construction (zero behavior change)."""

    client = make_mcp_client("transport-sentinel", client_cls=_FakeClient)

    assert client.target == "transport-sentinel"
    assert client.kwargs == {}
    assert not hasattr(client, "_clio_correlation_key")


def test_make_mcp_client_all_none_hooks_is_bare_construction() -> None:
    """An empty bundle (all hooks None) still yields a bare client."""

    client = make_mcp_client("t", handlers=MCPClientHandlers(), client_cls=_FakeClient)

    assert client.kwargs == {}
    assert not hasattr(client, "_clio_correlation_key")


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

    monkeypatch.setattr(
        "clio_agent.tools.mcp_config.transport_from_spec", fake_transport_from_spec
    )
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


def test_mapping_ambiguous_raises_value_error() -> None:
    """A mapping that is neither a CLIO spec nor an MCPConfig is a hard error."""

    with pytest.raises(ValueError, match="ambiguous MCP client target mapping"):
        make_mcp_client({"nonsense": 1, "keys": 2}, client_cls=_FakeClient)


# --------------------------------------------------------------------------- #
# Finding #5: message multiplexer preserves task dispatch AND calls the hook
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_message_multiplexer_runs_task_handler_then_hook() -> None:
    """The multiplexer preserves TaskNotificationHandler dispatch and fans out."""

    order: list[str] = []
    hook_context: list[Any] = []

    async def task_handler(message: Any) -> None:
        order.append(f"task:{message}")

    async def hook(context: Any, message: Any) -> None:
        order.append(f"hook:{message}")
        hook_context.append(context)

    registry = MCPCorrelationRegistry()
    ctx = MCPInvocationContext(invocation_id="i1", session_id="S1")
    registry.bind("key", ctx)

    mux = MessageMultiplexer(registry, "key", hook, task_handler)
    await mux("PING")

    # task handler runs FIRST (built-in routing preserved), then the CLIO hook,
    # which sees the correlated context.
    assert order == ["task:PING", "hook:PING"]
    assert hook_context == [ctx]


@pytest.mark.asyncio
async def test_message_multiplexer_bind_task_handler_late() -> None:
    """The task handler can be bound after construction (factory needs the client)."""

    seen: list[str] = []

    async def task_handler(message: Any) -> None:
        seen.append("task")

    async def hook(context: Any, message: Any) -> None:
        seen.append("hook")

    mux = MessageMultiplexer(MCPCorrelationRegistry(), "k", hook)
    assert mux._task_handler is None
    mux.bind_task_handler(task_handler)
    await mux("m")
    assert seen == ["task", "hook"]


# --------------------------------------------------------------------------- #
# Finding #6: correlation registry + two-sessions-one-executor seam
# --------------------------------------------------------------------------- #


def test_correlation_registry_stack_resolves_innermost() -> None:
    """Nested binds on one key resolve innermost-first, restoring the outer."""

    registry = MCPCorrelationRegistry()
    outer = MCPInvocationContext(invocation_id="o")
    inner = MCPInvocationContext(invocation_id="i")

    with registry.active("k", outer):
        assert registry.resolve("k") is outer
        with registry.active("k", inner):
            assert registry.resolve("k") is inner
        assert registry.resolve("k") is outer
    assert registry.resolve("k") is None


class _FiringClient:
    """Fake client that fires its elicitation handler mid-call (background loop)."""

    def __init__(self, target: Any, elicitation_handler: Any = None, **_kw: Any) -> None:
        self._elicit = elicitation_handler

    async def __aenter__(self) -> "_FiringClient":
        return self

    async def __aexit__(self, *exc: Any) -> None:
        return None

    async def list_tools(self) -> list[Any]:
        return []

    async def read_resource(self, uri: str) -> Any:
        return None

    async def call_tool(self, name: str, args: dict[str, Any]) -> Any:
        if self._elicit is not None:
            await self._elicit("continue?", None, None, None)
        return SimpleNamespace(data="ok", content=[])


@pytest.mark.asyncio
async def test_two_sessions_one_executor_resolve_correct_context() -> None:
    """A handler firing on a cached client resolves the right per-call context."""

    registry = MCPCorrelationRegistry()
    resolved: list[MCPInvocationContext | None] = []

    async def elicit(context: MCPInvocationContext | None, *a: Any) -> Any:
        resolved.append(context)
        return None

    handlers = MCPClientHandlers(elicitation=elicit, correlation=registry)

    def factory(target: Any) -> Any:
        return make_mcp_client(target, handlers=handlers, client_cls=_FiringClient)

    executor = AsyncMCPToolExecutor(
        object(),
        preloaded_tools={"ns_tool": object()},
        namespace_servers={"ns": object()},
        client_factory=factory,
    )
    await executor.start()

    token = current_invocation.set(MCPInvocationContext(invocation_id="a", session_id="A"))
    try:
        await executor.call_tool("ns_tool", {})
    finally:
        current_invocation.reset(token)

    token = current_invocation.set(MCPInvocationContext(invocation_id="b", session_id="B"))
    try:
        await executor.call_tool("ns_tool", {})
    finally:
        current_invocation.reset(token)

    # Same cached namespace client, two calls: each handler fire resolved its own
    # session, enriched with this call's namespace/tool -- not a stale ContextVar.
    assert [c.session_id for c in resolved if c] == ["A", "B"]
    assert [c.tool_name for c in resolved if c] == ["ns_tool", "ns_tool"]
    assert [c.namespace for c in resolved if c] == ["ns", "ns"]


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

    backend = FastMCP("backend")

    @backend.tool
    def ping() -> str:
        return "pong"

    async def elicit(context: Any, *a: Any) -> Any:
        return None

    backend_client = make_mcp_client(backend, handlers=MCPClientHandlers(elicitation=elicit))
    installed_cb = backend_client._session_kwargs["elicitation_callback"]

    proxy = FastMCP.as_proxy(backend_client)
    # The proxy runs client.new() per request; copy.copy carries the handler
    # onto that upstream client, so the callback reaches the real call path.
    upstream = proxy.client_factory()
    assert upstream._session_kwargs.get("elicitation_callback") is installed_cb


def test_proxy_for_spec_routes_backend_through_factory(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """_proxy_for_spec builds its backend client via make_mcp_client (with handlers)."""

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
        return Client(stub)  # a real client so as_proxy accepts it

    monkeypatch.setattr(gateway, "transport_for", lambda spec, cwd=None: "TSPORT")
    # gateway imports make_mcp_client function-locally; patch it at the source.
    monkeypatch.setattr("clio_agent.tools.mcp_runtime.make_mcp_client", spy)

    # as_proxy on a _FakeClient is fine; we only assert the factory was used.
    gateway._proxy_for_spec(
        MCPServerSpec(name="ext", transport="stdio", command="x"), handlers=handlers
    )

    assert calls == [("TSPORT", handlers)]


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
