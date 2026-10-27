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
