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

import weakref
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


class _StubTask:
    """Weakref-able stand-in for fastmcp's abstract Task in the registry."""

    def __init__(self) -> None:
        self.updates: list[Any] = []

    def _handle_status_notification(self, status: Any) -> None:
        self.updates.append(status)


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


def test_make_mcp_client_all_none_hooks_is_bare_construction() -> None:
    """An empty bundle (all hooks None) still yields a bare client."""

    client = make_mcp_client("t", handlers=MCPClientHandlers(), client_cls=_FakeClient)

    assert client.kwargs == {}


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
# Finding #5: message multiplexer preserves task dispatch AND calls the hook
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_message_multiplexer_runs_task_handler_then_hook() -> None:
    """The multiplexer preserves TaskNotificationHandler dispatch and fans out."""

    order: list[str] = []

    async def task_handler(message: Any) -> None:
        order.append(f"task:{message}")

    async def hook(context: Any, message: Any) -> None:
        order.append(f"hook:{message}")

    mux = MessageMultiplexer(hook, task_handler)
    await mux("PING")

    # task handler runs FIRST (built-in routing preserved), then the CLIO hook.
    assert order == ["task:PING", "hook:PING"]


@pytest.mark.asyncio
async def test_message_multiplexer_bind_task_handler_late() -> None:
    """The task handler can be bound after construction (factory needs the client)."""

    seen: list[str] = []

    async def task_handler(message: Any) -> None:
        seen.append("task")

    async def hook(context: Any, message: Any) -> None:
        seen.append("hook")

    mux = MessageMultiplexer(hook)
    assert mux._task_handler is None
    mux.bind_task_handler(task_handler)
    await mux("m")
    assert seen == ["task", "hook"]


@pytest.mark.asyncio
async def test_message_multiplexer_survives_client_clone() -> None:
    """A real client.new() clone rebinds the multiplexer to the CLONE.

    FastMCP proxies call a backend via client.new(); the clone's task handler
    must route task notifications to the CLONE (not the original), and the CLIO
    message hook must still fire. Exercises the real fastmcp Client + Task.
    """

    import mcp.types as mt
    from fastmcp import FastMCP

    fired: list[Any] = []

    async def hook(context: Any, message: Any) -> None:
        fired.append(message)

    client = make_mcp_client(FastMCP("backend"), handlers=MCPClientHandlers(message=hook))
    clone = client.new()

    mux = clone._session_kwargs["message_handler"]
    assert isinstance(mux, MessageMultiplexer)
    # the clone's task handler is bound to the CLONE, not the original client.
    assert mux._task_handler._client_ref() is clone

    # register a task on the CLONE, then deliver a status notification. A minimal
    # stub stands in for the abstract fastmcp Task; the client's registry routing
    # calls _handle_status_notification, exactly as a real Task would receive it.
    task = _StubTask()
    task_id = "task-1"
    clone._task_registry[task_id] = weakref.ref(task)
    notif = mt.ServerNotification(
        root=mt.TaskStatusNotification(
            params=mt.TaskStatusNotificationParams(
                taskId=task_id,
                status="completed",
                createdAt="2026-01-01T00:00:00Z",
                lastUpdatedAt="2026-01-01T00:00:01Z",
                ttl=60000,
            )
        )
    )
    await mux(notif)

    # the CLONE's registry routed the notification to its own task...
    assert len(task.updates) == 1
    assert task.updates[0].status == "completed"
    # ...and the CLIO hook still fired on the clone.
    assert fired == [notif]


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
