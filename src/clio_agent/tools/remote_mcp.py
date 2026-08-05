"""Catalog-projected remote MCP aliases backed by durable relay jobs."""

from __future__ import annotations

from collections.abc import Callable, Mapping
from contextlib import AbstractAsyncContextManager
from copy import deepcopy
from typing import Any, Protocol

from fastmcp import FastMCP
from fastmcp.tools import Tool, ToolResult
from mcp.types import Tool as McpTool

from clio_agent.tools.relay_transport import (
    RelayRemoteMcpCatalog,
    RelayRemoteMcpHandle,
)

REMOTE_MCP_NAMESPACE = "remote"
RELAY_FOLLOW_NAMESPACE = "relay"
REMOTE_MCP_JOB_HANDLE_OUTPUT_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "job_id": {"type": "string"},
        "state": {
            "type": "string",
            "enum": ["queued", "leased", "running", "succeeded", "failed", "canceled"],
        },
        "kind": {"type": "string", "const": "mcp_call"},
        "terminal": {"type": "boolean"},
        "catalog_revision": {"type": "string", "pattern": "^[0-9a-f]{64}$"},
    },
    "required": ["job_id", "state", "kind", "terminal", "catalog_revision"],
    "additionalProperties": False,
}

__all__ = [
    "REMOTE_MCP_NAMESPACE",
    "RELAY_FOLLOW_NAMESPACE",
    "REMOTE_MCP_JOB_HANDLE_OUTPUT_SCHEMA",
    "RemoteMcpFederation",
    "RemoteMcpRelayClient",
]


class RemoteMcpRelayClient(Protocol):
    """Open relay-client operations required by the federation projection."""

    async def discover_remote_mcp(self) -> RelayRemoteMcpCatalog:
        """Return the current relay-owned remote MCP catalog."""
        ...

    async def submit_remote_mcp(
        self,
        name: str,
        arguments: Mapping[str, Any],
        *,
        catalog_revision: str,
    ) -> RelayRemoteMcpHandle:
        """Submit one alias against the revision that advertised it."""
        ...

    async def call_relay_tool(
        self,
        name: str,
        arguments: Mapping[str, Any],
    ) -> Any:
        """Call a relay follow tool while retaining its typed result."""
        ...

    async def wait_for_submitted_job(
        self,
        job_id: str,
        *,
        timeout_seconds: float | None = None,
    ) -> Any | None:
        """Resolve a projected handle via its durable task record, or ``None``."""
        ...


RemoteMcpClientFactory = Callable[[], AbstractAsyncContextManager[RemoteMcpRelayClient]]


class _ProjectedRemoteMcpTool(Tool):
    """One relay-owned definition exposed below the gateway's ``remote`` mount."""

    def __init__(
        self,
        definition: McpTool,
        *,
        client_factory: RemoteMcpClientFactory,
        catalog_revision: str,
    ) -> None:
        alias = definition.name
        bare_name = alias.removeprefix(f"{REMOTE_MCP_NAMESPACE}_")
        if bare_name == alias or not bare_name:
            raise ValueError(f"relay remote MCP alias is not namespaced: {alias!r}")
        super().__init__(
            name=bare_name,
            title=definition.title,
            description=definition.description,
            parameters=deepcopy(definition.input_schema),
            output_schema=deepcopy(REMOTE_MCP_JOB_HANDLE_OUTPUT_SCHEMA),
            # Relay is the counterparty being classified. Its self-declared MCP
            # hints cannot prove a federated tool safe/read-only at CLIO's local
            # permission boundary; missing annotations intentionally take the
            # existing external/unvetted fail-safe path.
            annotations=None,
            meta=deepcopy(definition.meta),
        )
        self._alias = alias
        self._client_factory = client_factory
        self._catalog_revision = catalog_revision

    async def run(self, arguments: dict[str, Any]) -> ToolResult:
        """Submit the remote alias and return only its durable relay handle."""

        async with self._client_factory() as relay:
            handle = await relay.submit_remote_mcp(
                self._alias,
                arguments,
                catalog_revision=self._catalog_revision,
            )
        return ToolResult(structured_content=handle.to_wire())


_TASK_TO_JOB_STATE = {
    "completed": "succeeded",
    "failed": "failed",
    "cancelled": "canceled",
}


def _task_result_as_job_wire(job_id: str, resolved: Any) -> dict[str, Any]:
    """Project a terminal task outcome into relay_wait's job-record shape.

    ``resolved_via`` marks the divergence from relay's native path explicitly
    (no silent alternate route); state names map onto relay's job vocabulary.
    """

    status = str(getattr(resolved, "status", "") or "")
    state = _TASK_TO_JOB_STATE.get(status, status or "unknown")
    payload: dict[str, Any] = {
        "job": {
            "job_id": job_id,
            "state": state,
            "terminal": True,
        },
        "resolved_via": "serve_task_record",
    }
    result = getattr(resolved, "result", None)
    if result is not None:
        payload["result"] = result
    error = getattr(resolved, "error", None)
    if error is not None:
        payload["error"] = error
    return payload


class _ProjectedRelayFollowTool(Tool):
    """One relay-advertised bounded observation tool under the ``relay`` mount."""

    def __init__(self, definition: McpTool, *, client_factory: RemoteMcpClientFactory) -> None:
        bare_name = definition.name.removeprefix(f"{RELAY_FOLLOW_NAMESPACE}_")
        if bare_name == definition.name or not bare_name:
            raise ValueError(f"relay follow tool is not namespaced: {definition.name!r}")
        super().__init__(
            name=bare_name,
            title=definition.title,
            description=definition.description,
            parameters=deepcopy(definition.input_schema),
            output_schema=deepcopy(definition.output_schema),
            annotations=definition.annotations,
            meta=deepcopy(definition.meta),
        )
        self._relay_name = definition.name
        self._client_factory = client_factory

    async def run(self, arguments: dict[str, Any]) -> ToolResult:
        """Resolve serve-owned handles via their task records; else forward.

        A ``relay_wait`` on a job this process submitted resolves through the
        durable #1115 record (``task_id == job_id``): projected receipts carry
        no ``route_revision``, so relay's native follow path cannot route them
        to a remote cluster. Handles from anywhere else forward to relay
        untouched, typed failures included.
        """

        async with self._client_factory() as relay:
            if self._relay_name == "relay_wait":
                job_id = str((arguments or {}).get("job_id") or "").strip()
                resolver = getattr(relay, "wait_for_submitted_job", None)
                if job_id and callable(resolver):
                    timeout = (arguments or {}).get("timeout_seconds")
                    resolved = await resolver(job_id, timeout_seconds=timeout)
                    if resolved is not None:
                        return ToolResult(
                            structured_content=_task_result_as_job_wire(job_id, resolved)
                        )
            result = await relay.call_relay_tool(self._relay_name, arguments)
        return ToolResult(
            content=result.content,
            structured_content=result.structured_content,
            is_error=result.is_error,
        )


class RemoteMcpFederation:
    """Immutable projection of one relay catalog revision into a local tool server."""

    def __init__(
        self,
        catalog: RelayRemoteMcpCatalog,
        client_factory: RemoteMcpClientFactory,
    ) -> None:
        self._catalog = catalog
        self._client_factory = client_factory
        server = FastMCP("clio-remote-mcp-federation")
        for definition in catalog.tools.values():
            server.add_tool(
                _ProjectedRemoteMcpTool(
                    definition,
                    client_factory=client_factory,
                    catalog_revision=catalog.revision,
                )
            )
        self._server = server
        follow_server = FastMCP("clio-relay-federation-follow")
        for definition in catalog.follow_tools.values():
            follow_server.add_tool(
                _ProjectedRelayFollowTool(
                    definition,
                    client_factory=client_factory,
                )
            )
        self._follow_server = follow_server

    @classmethod
    async def discover(cls, client_factory: RemoteMcpClientFactory) -> "RemoteMcpFederation":
        """Read the relay catalog once and bind every projected call to its revision."""

        async with client_factory() as relay:
            catalog = await relay.discover_remote_mcp()
        return cls(catalog, client_factory)

    @property
    def catalog(self) -> RelayRemoteMcpCatalog:
        """The immutable relay catalog snapshot backing this projection."""

        return self._catalog

    @property
    def server(self) -> FastMCP:
        """The bare-name server mounted by the gateway as namespace ``remote``."""

        return self._server

    @property
    def follow_server(self) -> FastMCP:
        """The relay observation server mounted by the gateway as namespace ``relay``."""

        return self._follow_server
