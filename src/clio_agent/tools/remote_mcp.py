"""Catalog-projected remote MCP aliases backed by durable relay jobs."""

from __future__ import annotations

from collections.abc import Callable, Mapping
from contextlib import AbstractAsyncContextManager
from copy import deepcopy
from typing import Any, Protocol

from fastmcp import FastMCP
from fastmcp.tools import Tool, ToolResult
from mcp.types import Tool as McpTool

from clio_agent.tools.mcp_results import consume_dual_emission_twin
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
        # Populated only when a wait_for_terminal=True call resolved to a
        # terminal outcome (#1225 D1) -- absent, never null, on the plain
        # queued handle so the pre-existing wire shape stays byte-identical.
        "result": {},
        "error": {},
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

    async def observe_submitted_job(self, job_id: str) -> Any | None:
        """Resolve a projected handle via ONE observation of its durable record."""
        ...

    async def list_job_artifacts(self, job_id: str) -> list[dict[str, Any]]:
        """List one relay job's indexed artifact records, sizes included.

        Declared here (not just on the narrower ``RelayArtifactClient``
        protocol) because the same relay client factory this federation is
        built from is reused, unmodified, to mount the bounded artifact-fetch
        tool onto ``follow_server`` below -- the federation's client contract
        has to name every operation that mount actually calls.
        """
        ...

    async def fetch_artifact(self, artifact_id: str) -> bytes:
        """Return one relay artifact's decoded content bytes."""
        ...


RemoteMcpClientFactory = Callable[[], AbstractAsyncContextManager[RemoteMcpRelayClient]]


def _resolved_remote_mcp_wire(handle: RelayRemoteMcpHandle, resolved: Any) -> dict[str, Any]:
    """Fold a resolved #1115 task record onto a queued handle's wire shape.

    ``resolved`` is a ``ClientGetTaskResult`` (or the same-shaped stand-in
    :meth:`RelayTransportClient.wait_for_submitted_job` returns); its status
    maps through the SAME ``_TASK_TO_JOB_STATE`` table :func:`_task_result_as_job_wire`
    uses below, so a ``remote_*`` alias and the relay_wait/relay_observe follow
    tools agree on what "succeeded"/"failed"/"canceled" mean. A non-terminal
    status leaves ``terminal`` false and ``state`` at relay's reported name --
    never fabricated as settled. ``result``/``error`` are plain wire keys, not
    ``RelayRemoteMcpHandle`` fields (kept dataclass-free of them; #1225).
    """

    status = str(getattr(resolved, "status", "") or "")
    wire = handle.to_wire()
    wire["state"] = _TASK_TO_JOB_STATE.get(status, status or handle.state)
    wire["terminal"] = status in _TASK_TO_JOB_STATE
    result = getattr(resolved, "result", None)
    if result is not None:
        wire["result"] = consume_dual_emission_twin(result)
    error = getattr(resolved, "error", None)
    if error is not None:
        wire["error"] = error
    return wire


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
        """Submit the remote alias and, if requested, resolve it to terminal.

        ``wait_for_terminal`` is a DOOR-recognized argument, forwarded here
        via the alias's deepcopied ``inputSchema``. Before #1225 D1 it was
        sent to the door but then ignored on the way back -- the result was
        always the same hardcoded queued handle, forcing a separate
        ``relay_wait`` call and roughly doubling agent tool calls. One
        bounded follow-up through the durable #1115 record
        (``RelayTransportClient.wait_for_submitted_job``, reused not
        reimplemented -- the SAME machinery ``relay_wait`` itself uses)
        closes that gap.
        """

        async with self._client_factory() as relay:
            handle = await relay.submit_remote_mcp(
                self._alias, arguments, catalog_revision=self._catalog_revision
            )
            if not arguments.get("wait_for_terminal"):
                return ToolResult(structured_content=handle.to_wire())
            resolved = await relay.wait_for_submitted_job(
                handle.job_id, timeout_seconds=arguments.get("wait_timeout_seconds")
            )
        wire = handle.to_wire() if resolved is None else _resolved_remote_mcp_wire(handle, resolved)
        return ToolResult(structured_content=wire)


_TASK_TO_JOB_STATE = {
    "completed": "succeeded",
    "failed": "failed",
    "cancelled": "canceled",
}


def _task_result_as_job_wire(job_id: str, resolved: Any) -> dict[str, Any]:
    """Project a durable task record's observed outcome into the job-record shape.

    ``resolved_via`` marks the divergence from relay's native path explicitly
    (no silent alternate route); state names map onto relay's job vocabulary.
    ``terminal`` is read off ``resolved.status`` rather than assumed true --
    this projection now also carries ``relay_observe``'s ONE-SHOT peek
    (:meth:`~clio_agent.tools.relay_transport.RelayTransportClient.observe_submitted_job`),
    whose status can legitimately still be in flight (``queued`` / ``working`` /
    ``input_required``); only ``relay_wait``'s drive-to-terminal path is
    guaranteed terminal here.

    ``resolved.result`` is the durable #1115 record's own inlined task result,
    which can carry the relay door's standard MCP dual emission one hop
    inside this payload (a ``content[].text`` stringified fallback beside a
    ``structuredContent`` object). :func:`consume_dual_emission_twin` applies
    the same #832 clean-stream consume rule
    :func:`clio_agent.tools.mcp_results.call_tool_result_to_observer` applies
    at the top level -- the structured object is authoritative, a verified
    twin is dropped, and a genuinely distinct block is left untouched.
    """

    status = str(getattr(resolved, "status", "") or "")
    state = _TASK_TO_JOB_STATE.get(status, status or "unknown")
    payload: dict[str, Any] = {
        "job": {
            "job_id": job_id,
            "state": state,
            "terminal": status in _TASK_TO_JOB_STATE,
        },
        "resolved_via": "serve_task_record",
    }
    result = getattr(resolved, "result", None)
    if result is not None:
        payload["result"] = consume_dual_emission_twin(result)
    error = getattr(resolved, "error", None)
    if error is not None:
        payload["error"] = error
    return payload


def _cluster_is_route_handle(input_schema: Mapping[str, Any] | None) -> bool:
    """Whether a follow tool's ``cluster`` is half of a receipt-copied route.

    Relay's follow-tool schemas couple ``cluster`` and ``route_revision``
    through JSON Schema ``dependentRequired``: supplying either obliges the
    other. Where that coupling exists, ``cluster`` is not a value an agent may
    choose -- it is one field of a route handle copied verbatim off a
    submission receipt. Detected from the relay-supplied schema itself, so a
    relay that later drops or adds the coupling changes this answer without a
    tool-name list here needing an edit.
    """

    if not isinstance(input_schema, Mapping):
        return False
    dependent = input_schema.get("dependentRequired")
    if not isinstance(dependent, Mapping):
        return False
    required_with_cluster = dependent.get("cluster")
    return isinstance(required_with_cluster, (list, tuple)) and (
        "route_revision" in required_with_cluster
    )


def _with_cluster_hint(
    description: str | None,
    cluster_hint: str | None,
    *,
    input_schema: Mapping[str, Any] | None = None,
) -> str | None:
    """Append one cluster-identity sentence to a relay-owned follow-tool description.

    Suppressed when the tool's own schema makes ``cluster`` half of a route
    handle (:func:`_cluster_is_route_handle`). Naming the deployment's cluster
    beside such a tool reads as an invitation to pass it, and relay then
    refuses the call -- ``route_revision is required when cluster routes an
    existing job handle`` -- with a revision our handle-first ``jarvis_run``
    receipt does not carry. Observed live (#1195) on ``relay_observe``: the
    same job_id, called with no ``cluster`` at all, succeeds. Composed once
    from the resolved config value (``relay.cluster`` / ``CLIO_RELAY_CLUSTER``,
    see ``clio_agent.tools.relay_factory.resolve_relay_cluster``). Unset
    (``cluster_hint`` falsy) leaves the relay-supplied description
    byte-identical -- no placeholder text, per the no-silent-fallback rule.
    """

    if not cluster_hint or _cluster_is_route_handle(input_schema):
        return description
    sentence = f"This deployment's registered cluster is {cluster_hint!r}."
    return f"{description} {sentence}" if description else sentence


# Plain human names for the relay-owned follow tools. Relay advertises these
# without a title, so the UI head fell back to the raw wire name. Titles carry no
# parentheses -- the surrounding UI supplies arguments. Only the tools projected
# under this mount are named; a relay tool this mapping does not cover keeps
# whatever title relay itself declared.
_FOLLOW_TOOL_TITLES = {
    "relay_observe": "Observe Job",
    "relay_wait": "Wait For Job",
    "relay_artifact_lineage": "Artifact Lineage",
    "relay_status": "Relay Status",
}


class _ProjectedRelayFollowTool(Tool):
    """One relay-advertised bounded observation tool under the ``relay`` mount."""

    def __init__(
        self,
        definition: McpTool,
        *,
        client_factory: RemoteMcpClientFactory,
        cluster_hint: str | None = None,
    ) -> None:
        bare_name = definition.name.removeprefix(f"{RELAY_FOLLOW_NAMESPACE}_")
        if bare_name == definition.name or not bare_name:
            raise ValueError(f"relay follow tool is not namespaced: {definition.name!r}")
        super().__init__(
            name=bare_name,
            title=definition.title or _FOLLOW_TOOL_TITLES.get(definition.name),
            description=_with_cluster_hint(
                definition.description,
                cluster_hint,
                input_schema=definition.input_schema,
            ),
            parameters=deepcopy(definition.input_schema),
            output_schema=deepcopy(definition.output_schema),
            annotations=definition.annotations,
            meta=deepcopy(definition.meta),
        )
        self._relay_name = definition.name
        self._client_factory = client_factory

    async def run(self, arguments: dict[str, Any]) -> ToolResult:
        """Resolve serve-owned handles via their task records; else forward.

        Both ``relay_wait`` and ``relay_observe`` on a job this process
        submitted resolve through the durable #1115 record (``task_id ==
        job_id``): projected receipts carry no ``route_revision``, so relay's
        native follow path cannot route them to a remote cluster -- calling it
        for such a handle answers with one of relay's own untyped shapes
        (``job not found``, a ``route_revision`` schema error, ...) instead of
        the job's actual state. ``relay_wait`` drives the record to terminal
        (:meth:`~.relay_transport.RelayTransportClient.wait_for_submitted_job`);
        ``relay_observe`` takes ONE bounded peek at it
        (:meth:`~.relay_transport.RelayTransportClient.observe_submitted_job`)
        and never blocks. Handles from anywhere else forward to relay
        untouched, typed failures included.
        """

        resolver_name = {
            "relay_wait": "wait_for_submitted_job",
            "relay_observe": "observe_submitted_job",
        }.get(self._relay_name)
        async with self._client_factory() as relay:
            if resolver_name is not None:
                job_id = str((arguments or {}).get("job_id") or "").strip()
                resolver = getattr(relay, resolver_name, None)
                if job_id and callable(resolver):
                    if self._relay_name == "relay_wait":
                        timeout = (arguments or {}).get("timeout_seconds")
                        resolved = await resolver(job_id, timeout_seconds=timeout)
                    else:
                        resolved = await resolver(job_id)
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
        *,
        cluster_hint: str | None = None,
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
                    cluster_hint=cluster_hint,
                )
            )
        # clio-agent's OWN bounded artifact transfer (#1200), mounted beside the
        # relay-owned follow tools because it answers the same question they do
        # -- what came out of this job -- for the one case they cannot serve:
        # an output file that must be read locally rather than observed inline.
        from clio_agent.tools.relay_artifact_fetch import (  # noqa: PLC0415
            RelayArtifactFetchTool,
        )

        follow_server.add_tool(
            RelayArtifactFetchTool(client_factory=client_factory, cluster_hint=cluster_hint)
        )
        self._follow_server = follow_server

    @classmethod
    async def discover(
        cls,
        client_factory: RemoteMcpClientFactory,
        *,
        cluster_hint: str | None = None,
    ) -> "RemoteMcpFederation":
        """Read the relay catalog once and bind every projected call to its revision."""

        async with client_factory() as relay:
            catalog = await relay.discover_remote_mcp()
        return cls(catalog, client_factory, cluster_hint=cluster_hint)

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
