"""P2.12 acceptance."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Mapping
from contextlib import AbstractAsyncContextManager
from dataclasses import dataclass, field
from typing import Any

import pytest
from fastmcp import Client
from fastmcp.exceptions import ToolError
from mcp.types import CallToolResult, TextContent, Tool, ToolAnnotations

from clio_agent.tools.gateway import build_gateway, build_tool_catalog, list_tool_definitions
from clio_agent.tools.relay_transport import (
    RelayRemoteMcpCatalog,
    RelayRemoteMcpCatalogStaleError,
    RelayRemoteMcpHandle,
    RelayTransportClient,
)
from clio_agent.tools.remote_mcp import (
    REMOTE_MCP_JOB_HANDLE_OUTPUT_SCHEMA,
    RemoteMcpFederation,
)

CATALOG_REVISION = "a" * 64
NEXT_CATALOG_REVISION = "b" * 64


def _remote_tool(
    revision: str = CATALOG_REVISION,
    name: str = "remote_science_inspect",
) -> Tool:
    relay_receipt_schema = {
        **REMOTE_MCP_JOB_HANDLE_OUTPUT_SCHEMA,
        "properties": {
            **REMOTE_MCP_JOB_HANDLE_OUTPUT_SCHEMA["properties"],
            "cluster": {"type": "string"},
            "route_revision": {"type": "string"},
        },
        "required": [
            *REMOTE_MCP_JOB_HANDLE_OUTPUT_SCHEMA["required"],
            "cluster",
            "route_revision",
        ],
    }
    return Tool(
        name=name,
        description="Inspect a registered cluster dataset.",
        inputSchema={
            "type": "object",
            "properties": {
                "cluster": {"type": "string", "enum": ["ares"]},
                "request": {"type": "object"},
            },
            "required": ["cluster", "request"],
            "additionalProperties": False,
        },
        outputSchema=relay_receipt_schema,
        annotations=ToolAnnotations(
            readOnlyHint=True,
            destructiveHint=False,
            openWorldHint=False,
        ),
        _meta={"clio-relay/catalog-revision": revision},
    )


def _relay_wait_tool() -> Tool:
    return Tool(
        name="relay_wait",
        inputSchema={
            "type": "object",
            "properties": {"job_id": {"type": "string"}},
            "required": ["job_id"],
            "additionalProperties": False,
        },
        outputSchema={"type": "object"},
    )


@dataclass
class _CapturedMcpCallSpec:
    kind: str
    cluster: str
    tool: str
    arguments: dict[str, Any]
    catalog_revision: str


@dataclass
class _FakeRelayJob:
    """Lifecycle witness for one queued fake relay job and its event stream."""

    terminal: asyncio.Event = field(default_factory=asyncio.Event)
    stream_closed: asyncio.Event = field(default_factory=asyncio.Event)
    stream_task: asyncio.Task[None] | None = None


class _FakeRelayClient(AbstractAsyncContextManager["_FakeRelayClient"]):
    def __init__(self) -> None:
        self.catalog = RelayRemoteMcpCatalog(
            revision=CATALOG_REVISION,
            tools={
                "remote_science_inspect": _remote_tool(),
                "remote_science_oversized": _remote_tool(name="remote_science_oversized"),
            },
            follow_tools={"relay_wait": _relay_wait_tool()},
        )
        self.submitted_specs: list[_CapturedMcpCallSpec] = []
        self.jobs: dict[str, _FakeRelayJob] = {}

    async def __aenter__(self) -> "_FakeRelayClient":
        return self

    async def __aexit__(self, exc_type: Any, exc: Any, tb: Any) -> None:
        return None

    async def discover_remote_mcp(self) -> RelayRemoteMcpCatalog:
        return self.catalog

    async def submit_remote_mcp(
        self,
        name: str,
        arguments: Mapping[str, Any],
        *,
        catalog_revision: str,
    ) -> RelayRemoteMcpHandle:
        if self.catalog.revision != catalog_revision or name not in self.catalog.tools:
            raise RelayRemoteMcpCatalogStaleError(
                name,
                catalog_revision,
                self.catalog.revision,
            )
        remote_arguments = dict(arguments)
        cluster = str(remote_arguments.pop("cluster"))
        self.submitted_specs.append(
            _CapturedMcpCallSpec(
                kind="mcp_call",
                cluster=cluster,
                tool="inspect",
                arguments=remote_arguments,
                catalog_revision=catalog_revision,
            )
        )
        job_id = "job-oversized" if name.endswith("_oversized") else "job-1129"
        job = _FakeRelayJob()
        job.stream_task = asyncio.create_task(self._consume_event_stream(job))
        self.jobs[job_id] = job
        return RelayRemoteMcpHandle(
            job_id=job_id,
            state="queued",
            kind="mcp_call",
            terminal=False,
            catalog_revision=catalog_revision,
        )

    async def call_relay_tool(
        self,
        name: str,
        arguments: Mapping[str, Any],
    ) -> CallToolResult:
        assert name == "relay_wait"
        job_id = str(arguments["job_id"])
        await self._complete_job(job_id)
        if job_id == "job-oversized":
            payload = {
                "delivery": {
                    "schema_version": "clio-relay.mcp-result-delivery.v1",
                    "status": "failed",
                    "code": "inline_result_limit_exceeded",
                    "max_inline_bytes": 65_536,
                    "private_evidence_preserved": True,
                    "remote_side_effects_may_have_occurred": True,
                }
            }
            return CallToolResult(
                content=[TextContent(type="text", text="typed oversize failure")],
                structuredContent=payload,
                isError=True,
            )
        payload = {"job_id": job_id, "state": "succeeded", "result": "bounded-result"}
        return CallToolResult(
            content=[TextContent(type="text", text="bounded-result")],
            structuredContent=payload,
        )

    async def aclose(self) -> None:
        """Settle every fake job and drain its event-stream task."""

        for job_id in tuple(self.jobs):
            await self._complete_job(job_id)

    @staticmethod
    async def _consume_event_stream(job: _FakeRelayJob) -> None:
        """Model the relay stream poller that must finish with its fake job."""

        try:
            while not job.terminal.is_set():
                await asyncio.sleep(0.01)
        finally:
            job.stream_closed.set()

    async def _complete_job(self, job_id: str) -> None:
        job = self.jobs[job_id]
        job.terminal.set()
        if job.stream_task is not None:
            await job.stream_task


@pytest.fixture
async def fake_relay() -> AsyncIterator[_FakeRelayClient]:
    """Always drain fake relay streams, including when an assertion fails."""

    relay = _FakeRelayClient()
    try:
        yield relay
    finally:
        await relay.aclose()
        assert all(job.stream_closed.is_set() for job in relay.jobs.values())
        assert all(
            job.stream_task is not None and job.stream_task.done() for job in relay.jobs.values()
        )


class _CatalogMcpClient:
    def __init__(self, catalog: RelayRemoteMcpCatalog) -> None:
        self.catalog = catalog

    async def list_tools_mcp(self, *, cursor: str | None = None) -> Any:
        assert cursor is None
        return type(
            "CatalogPage",
            (),
            {
                "meta": {"clio-relay/remote-mcp-catalog-revision": self.catalog.revision},
                "tools": list(self.catalog.tools.values()),
                "next_cursor": None,
            },
        )()


class _RecordingRelayTransport(RelayTransportClient):
    def __init__(self, catalog: RelayRemoteMcpCatalog) -> None:
        super().__init__(
            "http://relay.invalid/mcp",
            "http://relay.invalid",
            api_token="test-token",
        )
        self._mcp_client = _CatalogMcpClient(catalog)
        self.submissions: list[tuple[str, Mapping[str, Any]]] = []

    async def submit(
        self,
        tool_name: str,
        arguments: Mapping[str, Any] | None = None,
        *,
        idempotency_key: str | None = None,
        timeout_seconds: float | None = None,
    ) -> Any:
        del idempotency_key, timeout_seconds
        self.submissions.append((tool_name, dict(arguments or {})))
        raise AssertionError("stale catalog reached relay submission")


@pytest.mark.asyncio
async def test_virtual_alias_submits_mcp_call_spec_and_returns_handle(
    fake_relay: _FakeRelayClient,
) -> None:
    """FAILING-FIRST: the projected alias submits one exact durable mcp_call spec."""

    relay = fake_relay
    federation = await RemoteMcpFederation.discover(lambda: relay)
    gateway = build_gateway({}, remote_mcp_federation=federation)
    definitions = list_tool_definitions(gateway)
    catalog = build_tool_catalog(gateway, tools=list(definitions.values()))
    request = {
        "path": "/datasets/run-001.h5",
        "options": {"fields": ["temperature", "velocity"], "limit": 0},
        "enabled": False,
        "note": "lambda-byte-untouched",
    }

    async with Client(gateway) as client:
        listed = {tool.name: tool for tool in await client.list_tools()}
        result = await client.call_tool(
            "remote_science_inspect",
            {"cluster": "ares", "request": request},
        )
        waited = await client.call_tool("relay_wait", {"job_id": "job-1129"})

    projected = listed["remote_science_inspect"]
    assert projected.output_schema == REMOTE_MCP_JOB_HANDLE_OUTPUT_SCHEMA
    assert catalog["remote_science_inspect"].owner == "remote"
    assert "read" not in catalog["remote_science_inspect"].tags
    assert {
        field: getattr(result.data, field)
        for field in ("job_id", "state", "kind", "terminal", "catalog_revision")
    } == {
        "job_id": "job-1129",
        "state": "queued",
        "kind": "mcp_call",
        "terminal": False,
        "catalog_revision": CATALOG_REVISION,
    }
    assert relay.submitted_specs == [
        _CapturedMcpCallSpec(
            kind="mcp_call",
            cluster="ares",
            tool="inspect",
            arguments={"request": request},
            catalog_revision=CATALOG_REVISION,
        )
    ]
    assert waited.data["job_id"] == "job-1129"
    assert waited.data["state"] == "succeeded"
    assert waited.data["result"] == "bounded-result"
    assert relay.jobs["job-1129"].stream_closed.is_set()
    assert relay.jobs["job-1129"].stream_task is not None
    assert relay.jobs["job-1129"].stream_task.done()


@pytest.mark.asyncio
async def test_cluster_hint_stamps_relay_follow_tool_only(fake_relay: _FakeRelayClient) -> None:
    """FAILING-FIRST (#1171 cluster-discovery gap): CLIO_RELAY_CLUSTER's resolved
    value reaches relay_wait's (a relay-follow tool's) description verbatim,
    composed once at construction -- and never touches the remote_* alias
    descriptions, which stay relay-owned and byte-untouched."""

    federation = await RemoteMcpFederation.discover(lambda: fake_relay, cluster_hint="ares-p5run2")
    gateway = build_gateway({}, remote_mcp_federation=federation)

    async with Client(gateway) as client:
        listed = {tool.name: tool for tool in await client.list_tools()}

    assert listed["relay_wait"].description == (
        "This deployment's registered cluster is 'ares-p5run2'."
    )
    assert listed["remote_science_inspect"].description == "Inspect a registered cluster dataset."


@pytest.mark.asyncio
async def test_cluster_hint_unset_leaves_relay_follow_description_unchanged(
    fake_relay: _FakeRelayClient,
) -> None:
    """Unset cluster_hint (the default) -> relay_wait's relay-supplied description
    stays exactly what the fixture declared -- no placeholder, no sentence
    (no-silent-fallback: nothing else about the description changes)."""

    federation = await RemoteMcpFederation.discover(lambda: fake_relay)
    gateway = build_gateway({}, remote_mcp_federation=federation)

    async with Client(gateway) as client:
        listed = {tool.name: tool for tool in await client.list_tools()}

    assert listed["relay_wait"].description == _relay_wait_tool().description
    assert (listed["relay_wait"].description or "") == ""
    assert listed["remote_science_inspect"].description == "Inspect a registered cluster dataset."


@pytest.mark.asyncio
async def test_remote_alias_cannot_self_declare_read_only(
    fake_relay: _FakeRelayClient,
) -> None:
    """Finding 7: relay annotations never bypass the external permission boundary."""

    federation = await RemoteMcpFederation.discover(lambda: fake_relay)
    gateway = build_gateway({}, remote_mcp_federation=federation)
    definitions = list_tool_definitions(gateway)
    catalog = build_tool_catalog(gateway, tools=list(definitions.values()))

    async with Client(gateway) as client:
        projected = {tool.name: tool for tool in await client.list_tools()}[
            "remote_science_inspect"
        ]

    assert projected.annotations is None
    assert "read" not in catalog["remote_science_inspect"].tags


@pytest.mark.asyncio
async def test_oversize_result_is_typed_and_never_truncated(
    fake_relay: _FakeRelayClient,
) -> None:
    """relay_wait preserves the public 64 KiB delivery failure as an error result."""

    relay = fake_relay
    federation = await RemoteMcpFederation.discover(lambda: relay)
    gateway = build_gateway({}, remote_mcp_federation=federation)

    async with Client(gateway) as client:
        handle = await client.call_tool(
            "remote_science_oversized",
            {"cluster": "ares", "request": {"path": "/data/large"}},
        )
        waited = await client.call_tool_mcp(
            "relay_wait",
            {"job_id": handle.data.job_id},
        )

    assert waited.is_error is True
    assert waited.structured_content == {
        "delivery": {
            "schema_version": "clio-relay.mcp-result-delivery.v1",
            "status": "failed",
            "code": "inline_result_limit_exceeded",
            "max_inline_bytes": 65_536,
            "private_evidence_preserved": True,
            "remote_side_effects_may_have_occurred": True,
        }
    }
    assert "content_truncated" not in waited.structured_content
    assert relay.jobs["job-oversized"].stream_closed.is_set()
    assert relay.jobs["job-oversized"].stream_task is not None
    assert relay.jobs["job-oversized"].stream_task.done()


@pytest.mark.asyncio
async def test_stale_catalog_revision_is_typed_before_mcp_call_submission(
    fake_relay: _FakeRelayClient,
) -> None:
    """A catalog edit after projection fails before the relay resolves or submits a route."""

    relay = fake_relay
    federation = await RemoteMcpFederation.discover(lambda: relay)
    relay.catalog = RelayRemoteMcpCatalog(
        revision=NEXT_CATALOG_REVISION,
        tools={"remote_science_inspect": _remote_tool(NEXT_CATALOG_REVISION)},
    )

    async with Client(federation.server) as client:
        with pytest.raises(ToolError, match="catalog changed after local tool projection"):
            await client.call_tool(
                "science_inspect",
                {"cluster": "ares", "request": {"path": "/data/run"}},
            )

    assert relay.submitted_specs == []


@pytest.mark.asyncio
async def test_transport_rejects_stale_revision_before_submit() -> None:
    """RelayTransportClient performs the revision guard before call_tool_task."""

    current = RelayRemoteMcpCatalog(
        revision=NEXT_CATALOG_REVISION,
        tools={"remote_science_inspect": _remote_tool(NEXT_CATALOG_REVISION)},
    )
    relay = _RecordingRelayTransport(current)

    with pytest.raises(RelayRemoteMcpCatalogStaleError) as raised:
        await relay.submit_remote_mcp(
            "remote_science_inspect",
            {"cluster": "ares", "request": {"path": "/data/run"}},
            catalog_revision=CATALOG_REVISION,
        )

    assert raised.value.reason == "remote_mcp_catalog_revision_stale"
    assert raised.value.details["expected_catalog_revision"] == CATALOG_REVISION
    assert raised.value.details["observed_catalog_revision"] == NEXT_CATALOG_REVISION
    assert relay.submissions == []


class _ResolvedTask:
    """Terminal ClientGetTaskResult stand-in for the record-store resolver."""

    def __init__(self, status: str = "completed", result: Any = None, error: Any = None) -> None:
        self.status = status
        self.result = result
        self.error = error


class _RecordResolvingRelayClient(_FakeRelayClient):
    """Fake relay whose transport owns a persisted record for one job."""

    def __init__(self, known_job_id: str) -> None:
        super().__init__()
        self.known_job_id = known_job_id
        self.resolved_calls: list[tuple[str, Any]] = []

    async def wait_for_submitted_job(
        self, job_id: str, *, timeout_seconds: Any = None
    ) -> _ResolvedTask | None:
        self.resolved_calls.append((job_id, timeout_seconds))
        if job_id != self.known_job_id:
            return None
        return _ResolvedTask(status="completed", result={"content": "task-door-result"})


@pytest.mark.asyncio
async def test_relay_wait_resolves_serve_owned_handle_via_task_record() -> None:
    """A relay_wait on a job this process submitted resolves through the durable
    task record (projected receipts carry no route_revision, so relay's native
    follow path cannot route them) — with the divergence marked explicitly."""

    relay = _RecordResolvingRelayClient(known_job_id="job-1129")
    federation = await RemoteMcpFederation.discover(lambda: relay)
    gateway = build_gateway({}, remote_mcp_federation=federation)

    async with Client(gateway) as client:
        waited = await client.call_tool(
            "relay_wait", {"job_id": "job-1129", "timeout_seconds": 120}
        )

    assert relay.resolved_calls == [("job-1129", 120)]
    assert waited.data["job"]["job_id"] == "job-1129"
    assert waited.data["job"]["state"] == "succeeded"
    assert waited.data["job"]["terminal"] is True
    assert waited.data["resolved_via"] == "serve_task_record"
    assert waited.data["result"] == {"content": "task-door-result"}


def _remote_tool_with_wait_support(
    revision: str = CATALOG_REVISION,
    name: str = "remote_science_inspect",
) -> Tool:
    """Same alias ``_remote_tool`` projects, but with the door's real
    wait_for_terminal input properties (captured live:
    ``RELAY_SUBMIT_AGENT_SCHEMA_PROPERTIES_FIXTURE``,
    ``tests/test_gact/test_relay_invoker_runtime_contract.py``) -- the wire
    shape #1225 D1 actually has to cope with."""

    relay_receipt_schema = {
        **REMOTE_MCP_JOB_HANDLE_OUTPUT_SCHEMA,
        "properties": {
            **REMOTE_MCP_JOB_HANDLE_OUTPUT_SCHEMA["properties"],
            "cluster": {"type": "string"},
            "route_revision": {"type": "string"},
        },
        "required": [
            *REMOTE_MCP_JOB_HANDLE_OUTPUT_SCHEMA["required"],
            "cluster",
            "route_revision",
        ],
    }
    return Tool(
        name=name,
        description="Inspect a registered cluster dataset.",
        inputSchema={
            "type": "object",
            "properties": {
                "cluster": {"type": "string", "enum": ["ares"]},
                "request": {"type": "object"},
                "wait_for_terminal": {"type": "boolean", "default": False},
                "wait_timeout_seconds": {"type": "number"},
            },
            "required": ["cluster", "request"],
            "additionalProperties": False,
        },
        outputSchema=relay_receipt_schema,
        annotations=ToolAnnotations(
            readOnlyHint=True,
            destructiveHint=False,
            openWorldHint=False,
        ),
        _meta={"clio-relay/catalog-revision": revision},
    )


@pytest.mark.asyncio
async def test_wait_for_terminal_resolves_without_a_separate_relay_wait_call() -> None:
    """FAILING-FIRST for #1225 D1.

    Live L3 evidence: the agent passed ``wait_for_terminal: True`` on a relay
    tool call; the call still came back as a queued handle, forcing a
    separate ``relay_wait`` round trip for every such call and roughly
    doubling tool calls until the agent burned its turn budget. Before the
    fix, ``_ProjectedRemoteMcpTool.run`` forwarded ``wait_for_terminal`` into
    the submission payload and then unconditionally returned the same
    hardcoded ``state="queued"/terminal=False`` handle, discarding whatever
    the door actually did with it. After the fix, a wait_for_terminal=True
    call resolves through ONE follow-up call to
    ``wait_for_submitted_job`` -- the SAME durable-record machinery
    ``relay_wait`` itself uses -- and no separate ``relay_wait`` call is
    needed at all.
    """

    relay = _RecordResolvingRelayClient(known_job_id="job-1129")
    relay.catalog = RelayRemoteMcpCatalog(
        revision=relay.catalog.revision,
        tools={"remote_science_inspect": _remote_tool_with_wait_support()},
        follow_tools=relay.catalog.follow_tools,
    )
    federation = await RemoteMcpFederation.discover(lambda: relay)
    gateway = build_gateway({}, remote_mcp_federation=federation)

    async with Client(gateway) as client:
        result = await client.call_tool(
            "remote_science_inspect",
            {
                "cluster": "ares",
                "request": {"path": "/data/run"},
                "wait_for_terminal": True,
            },
        )

    # The resolution went through wait_for_submitted_job exactly once -- no
    # separate relay_wait tool call was made anywhere in this test.
    assert relay.resolved_calls == [("job-1129", None)]
    assert result.data.job_id == "job-1129"
    assert result.data.terminal is True
    assert result.data.state == "succeeded"
    assert result.data.result == {"content": "task-door-result"}


@pytest.mark.asyncio
async def test_wait_for_terminal_false_keeps_the_plain_queued_handle() -> None:
    """No regression: omitting wait_for_terminal keeps the pre-existing,
    byte-identical queued handle shape -- no result/error keys appended."""

    relay = _RecordResolvingRelayClient(known_job_id="job-1129")
    relay.catalog = RelayRemoteMcpCatalog(
        revision=relay.catalog.revision,
        tools={"remote_science_inspect": _remote_tool_with_wait_support()},
        follow_tools=relay.catalog.follow_tools,
    )
    federation = await RemoteMcpFederation.discover(lambda: relay)
    gateway = build_gateway({}, remote_mcp_federation=federation)

    async with Client(gateway) as client:
        result = await client.call_tool(
            "remote_science_inspect",
            {"cluster": "ares", "request": {"path": "/data/run"}},
        )

    assert relay.resolved_calls == []
    assert result.data.job_id == "job-1129"
    assert result.data.terminal is False
    assert result.data.state == "queued"
    assert "result" not in result.structured_content
    assert "error" not in result.structured_content


@pytest.mark.asyncio
async def test_relay_wait_forwards_foreign_handles_untouched() -> None:
    """A job_id with no persisted record falls through to relay's native
    relay_wait, byte-identical to the pre-resolver behavior."""

    relay = _RecordResolvingRelayClient(known_job_id="job-other")
    federation = await RemoteMcpFederation.discover(lambda: relay)
    gateway = build_gateway({}, remote_mcp_federation=federation)

    async with Client(gateway) as client:
        await client.call_tool("remote_science_inspect", {"cluster": "ares", "request": {}})
        waited = await client.call_tool("relay_wait", {"job_id": "job-1129"})

    # The resolver was consulted and declined; the door's native path answered.
    assert relay.resolved_calls == [("job-1129", None)]
    assert waited.data["state"] == "succeeded"
    assert waited.data["result"] == "bounded-result"


class _ObserveResolvingRelayClient(_FakeRelayClient):
    """Fake relay whose transport owns a persisted record for one job, exercised
    through ``relay_observe``'s ONE-SHOT resolution path rather than
    ``relay_wait``'s drive-to-terminal.

    ``call_relay_tool`` reproduces relay's own untyped ``relay_observe``
    failure for a locally-owned SEP-2663 handle it cannot route (#1195-class:
    "job not found" / a ``route_revision`` schema error) -- the exact
    inconsistency this fix's local resolution is meant to bypass for a job
    this process's own #1115 record already knows about.
    """

    def __init__(self, known_job_id: str) -> None:
        super().__init__()
        self.known_job_id = known_job_id
        self.observed_calls: list[str] = []

    async def observe_submitted_job(self, job_id: str) -> "_ResolvedTask | None":
        self.observed_calls.append(job_id)
        if job_id != self.known_job_id:
            return None
        return _ResolvedTask(status="working", result=None)

    async def call_relay_tool(self, name: str, arguments: Mapping[str, Any]) -> CallToolResult:
        assert name == "relay_observe"
        job_id = str(arguments["job_id"])
        return CallToolResult(
            content=[TextContent(type="text", text=f"job not found: {job_id}")],
            structuredContent={"error": f"job not found: {job_id}"},
            isError=True,
        )


@pytest.mark.asyncio
async def test_relay_observe_resolves_serve_owned_handle_via_task_record() -> None:
    """relay_observe now gets the SAME local-record resolution relay_wait has
    (previously it always forwarded to relay's native follow tool, which
    cannot route a locally-owned handle and answers with one of its own
    untyped shapes -- #1195-class 'job not found' / route_revision errors).
    A known job_id resolves through the durable #1115 record via ONE
    observation, never reaching relay's native tool at all.
    """

    relay = _ObserveResolvingRelayClient(known_job_id="job-1129")
    relay.catalog = RelayRemoteMcpCatalog(
        revision=relay.catalog.revision,
        tools=relay.catalog.tools,
        follow_tools={"relay_observe": _relay_observe_tool(), "relay_wait": _relay_wait_tool()},
    )
    federation = await RemoteMcpFederation.discover(lambda: relay)
    gateway = build_gateway({}, remote_mcp_federation=federation)

    async with Client(gateway) as client:
        observed = await client.call_tool("relay_observe", {"job_id": "job-1129"})

    assert relay.observed_calls == ["job-1129"]
    assert observed.data["job"]["job_id"] == "job-1129"
    assert observed.data["job"]["state"] == "working"
    assert observed.data["job"]["terminal"] is False
    assert observed.data["resolved_via"] == "serve_task_record"


@pytest.mark.asyncio
async def test_relay_observe_forwards_foreign_handles_untouched() -> None:
    """A job_id with no persisted record falls through to relay's native
    relay_observe, byte-identical to the pre-resolver behavior -- matching
    relay_wait's own forwarding contract for a handle this store never
    durably recorded."""

    relay = _ObserveResolvingRelayClient(known_job_id="job-1129")
    relay.catalog = RelayRemoteMcpCatalog(
        revision=relay.catalog.revision,
        tools=relay.catalog.tools,
        follow_tools={"relay_observe": _relay_observe_tool(), "relay_wait": _relay_wait_tool()},
    )
    federation = await RemoteMcpFederation.discover(lambda: relay)
    gateway = build_gateway({}, remote_mcp_federation=federation)

    async with Client(gateway) as client:
        with pytest.raises(ToolError, match="job not found: job-other"):
            await client.call_tool("relay_observe", {"job_id": "job-other"})

    assert relay.observed_calls == ["job-other"]


def _relay_observe_tool() -> Tool:
    """relay_observe exactly as the live p5run2 relay advertises it (#1195).

    The load-bearing part is ``dependentRequired``: supplying ``cluster``
    obliges ``route_revision``, so ``cluster`` here is one half of a route
    handle copied off a submission receipt -- not a value a caller may pick.
    """

    return Tool(
        name="relay_observe",
        description=(
            "Read job events from a cursor and optionally return when a regex pattern "
            "matches stdout, stderr, or event text. For a remote job, copy cluster, "
            "job_id, and route_revision unchanged from its submission receipt on every "
            "follow-up call, including on the same MCP connection. job_id alone is only "
            "for a local relay job."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "job_id": {"type": "string"},
                "cluster": {"type": "string"},
                "route_revision": {"type": "string", "pattern": "^[0-9a-f]{64}$"},
                "include_logs": {"type": "boolean", "default": True},
            },
            "required": ["job_id"],
            "dependentRequired": {
                "cluster": ["route_revision"],
                "route_revision": ["cluster"],
            },
            "additionalProperties": False,
        },
        outputSchema={"type": "object"},
    )


@pytest.mark.asyncio
async def test_cluster_hint_never_stamps_a_route_handle_follow_tool(
    fake_relay: _FakeRelayClient,
) -> None:
    """FAILING-FIRST (#1195): naming the cluster beside relay_observe broke it live.

    relay_observe couples ``cluster`` with ``route_revision`` via
    ``dependentRequired``. Appending "This deployment's registered cluster is
    'ares-p5run2'." invited the agent to pass ``cluster``, and relay then
    refused the call -- ``route_revision is required when cluster routes an
    existing job handle`` -- with a revision the handle-first ``jarvis_run``
    receipt never carries. Live check on the same relay: the identical job_id
    with NO cluster succeeds. So the hint must be suppressed here and relay's
    own description must survive byte-identical.
    """

    observe = _relay_observe_tool()
    fake_relay.catalog = RelayRemoteMcpCatalog(
        revision=fake_relay.catalog.revision,
        tools=fake_relay.catalog.tools,
        follow_tools={"relay_observe": observe, "relay_wait": _relay_wait_tool()},
    )

    federation = await RemoteMcpFederation.discover(lambda: fake_relay, cluster_hint="ares-p5run2")
    gateway = build_gateway({}, remote_mcp_federation=federation)

    async with Client(gateway) as client:
        listed = {tool.name: tool for tool in await client.list_tools()}

    assert listed["relay_observe"].description == observe.description
    assert "ares-p5run2" not in (listed["relay_observe"].description or "")
    # A follow tool whose cluster is NOT a route handle still gets the hint --
    # the suppression is decided by the schema's coupling, not by tool name.
    assert listed["relay_wait"].description == (
        "This deployment's registered cluster is 'ares-p5run2'."
    )


def _relay_status_tool() -> Tool:
    """relay_status exactly as the live p5run2 relay door advertises it
    (captured tools/list name, RELAY_DOOR_TOOLS_LIST_FIXTURE in
    tests/test_gact/test_relay_invoker_runtime_contract.py)."""

    return Tool(
        name="relay_status",
        description="Report relay cluster and queue health.",
        inputSchema={"type": "object", "properties": {}, "additionalProperties": False},
        outputSchema={"type": "object"},
    )


def _relay_artifact_lineage_tool() -> Tool:
    """relay_artifact_lineage exactly as the live p5run2 relay door advertises
    it (same captured fixture) -- case07-S3 needs this for artifact
    provenance."""

    return Tool(
        name="relay_artifact_lineage",
        description="Return the provenance chain for one artifact.",
        inputSchema={
            "type": "object",
            "properties": {"artifact_id": {"type": "string"}},
            "required": ["artifact_id"],
            "additionalProperties": False,
        },
        outputSchema={"type": "object"},
    )


@pytest.mark.asyncio
async def test_relay_status_and_artifact_lineage_are_projected(
    fake_relay: _FakeRelayClient,
) -> None:
    """FAILING-FIRST for #1228 D3.

    The live door's real tools/list catalog (captured, #1221 investigation --
    ``RELAY_DOOR_TOOLS_LIST_FIXTURE`` in
    ``tests/test_gact/test_relay_invoker_runtime_contract.py``) advertises
    both ``relay_artifact_lineage`` and ``relay_status``. Before the fix,
    neither name was a member of ``REMOTE_MCP_FOLLOW_TOOLS``, so
    ``discover_remote_mcp`` silently dropped both on the floor -- not a
    projected ``remote_*`` alias (wrong prefix) and not a recognized follow
    tool, just skipped without a trace. An agent ACL naming either then
    bricked the WHOLE session (``custom_agent_tools_unavailable`` /
    ``not_implemented``) instead of getting a working, read-only tool --
    lineage matters for case07-S3 artifact provenance.
    """

    fake_relay.catalog = RelayRemoteMcpCatalog(
        revision=fake_relay.catalog.revision,
        tools=fake_relay.catalog.tools,
        follow_tools={
            "relay_wait": _relay_wait_tool(),
            "relay_status": _relay_status_tool(),
            "relay_artifact_lineage": _relay_artifact_lineage_tool(),
        },
    )

    federation = await RemoteMcpFederation.discover(lambda: fake_relay)

    assert "relay_status" in federation.catalog.follow_tools
    assert "relay_artifact_lineage" in federation.catalog.follow_tools

    gateway = build_gateway({}, remote_mcp_federation=federation)
    async with Client(gateway) as client:
        listed = {tool.name: tool for tool in await client.list_tools()}

    assert "relay_status" in listed
    assert "relay_artifact_lineage" in listed
    assert listed["relay_status"].title == "Relay Status"
    assert listed["relay_artifact_lineage"].title == "Artifact Lineage"


def _relay_read_artifact_tool() -> Tool:
    """relay_read_artifact as the live ares door advertises it (develop @
    86714c4, mcp_server.py:1299) -- the bounded artifact-content fetch."""

    return Tool(
        name="relay_read_artifact",
        description="Fetch one registered artifact's bounded content by artifact_id.",
        inputSchema={
            "type": "object",
            "properties": {"artifact_id": {"type": "string"}},
            "required": ["artifact_id"],
            "additionalProperties": False,
        },
        outputSchema={"type": "object"},
    )


@pytest.mark.asyncio
async def test_relay_read_artifact_is_projected(fake_relay: _FakeRelayClient) -> None:
    """FAILING-FIRST for the 2026-08-19 ares L3 clause-(d) gap.

    The door declares AND dispatches ``relay_read_artifact`` (the bounded
    ``read_artifact_bytes`` fetch), but the name was absent from
    ``REMOTE_MCP_FOLLOW_TOOLS``, so the agent's surface had NO tool that
    returns artifact content -- the grounded L3 run located the real
    ``log.lammps`` thermo artifact via lineage metadata and could not read
    it (l3-run-20260819T064512.json: the agent correctly refused to
    fabricate the physics). Same defect class as #1228 D3, one tool short.
    """

    fake_relay.catalog = RelayRemoteMcpCatalog(
        revision=fake_relay.catalog.revision,
        tools=fake_relay.catalog.tools,
        follow_tools={
            "relay_wait": _relay_wait_tool(),
            "relay_read_artifact": _relay_read_artifact_tool(),
        },
    )

    federation = await RemoteMcpFederation.discover(lambda: fake_relay)

    assert "relay_read_artifact" in federation.catalog.follow_tools

    gateway = build_gateway({}, remote_mcp_federation=federation)
    async with Client(gateway) as client:
        listed = {tool.name: tool for tool in await client.list_tools()}

    assert "relay_read_artifact" in listed
    assert listed["relay_read_artifact"].title == "Read Artifact"


def _relay_list_artifacts_tool() -> Tool:
    """relay_list_artifacts as the door advertises it -- one stable page of a
    job's registered artifacts (the produced-outputs DISCOVERY half; the
    2026-08-19 L3 retry proved lineage's job direction lists consumed inputs
    only, so without this tool the relay-minted execution_output ids are
    unreachable from any job_id)."""

    return Tool(
        name="relay_list_artifacts",
        description="List one stable page of a job's registered artifacts.",
        inputSchema={
            "type": "object",
            "properties": {"job_id": {"type": "string"}},
            "required": ["job_id"],
            "additionalProperties": False,
        },
        outputSchema={"type": "object"},
    )


@pytest.mark.asyncio
async def test_relay_list_artifacts_is_projected(fake_relay: _FakeRelayClient) -> None:
    """FAILING-FIRST: the discovery half of the produced-content path."""

    fake_relay.catalog = RelayRemoteMcpCatalog(
        revision=fake_relay.catalog.revision,
        tools=fake_relay.catalog.tools,
        follow_tools={
            "relay_wait": _relay_wait_tool(),
            "relay_list_artifacts": _relay_list_artifacts_tool(),
        },
    )

    federation = await RemoteMcpFederation.discover(lambda: fake_relay)

    assert "relay_list_artifacts" in federation.catalog.follow_tools

    gateway = build_gateway({}, remote_mcp_federation=federation)
    async with Client(gateway) as client:
        listed = {tool.name: tool for tool in await client.list_tools()}

    assert "relay_list_artifacts" in listed
    assert listed["relay_list_artifacts"].title == "List Artifacts"
