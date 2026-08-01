"""The clio-relay transport client: two doors behind one durable task surface.

The MCP door is deliberately the existing #1115 SEP-2663 client.  Submission uses
``fastmcp_tasks.call_tool_task`` only to obtain the handle, immediately writes that
handle through #1115's shared durable-record seam, and drives status, reconnect, and
ack-only cancellation through #1115's named task requests and process-local lease.
Timeline events and artifact bytes use relay's authenticated HTTP door; SSE is an
observation channel and never becomes a second task-state driver.

Relay accepts an ``idempotency_key`` tool argument, so callers can supply a stable
client token when retry de-duplication is intentional.  Omitting it preserves relay's
documented fresh-run behavior and the exact #1115 residual: a crash after relay
admission but before the returned task id is durably recorded cannot be rediscovered
because SEP-2663 has no ``tasks/list``.  The shared durability seam keeps that window
minimal and fails loudly with ``mcp_task_record_not_durable`` if the write itself fails.
"""

from __future__ import annotations

import base64
import binascii
import json
import os
from collections.abc import AsyncIterator, Iterator, Mapping
from dataclasses import dataclass, replace
from dataclasses import field as dataclass_field
from typing import Any, Literal
from urllib.parse import quote

import httpx
from fastmcp_tasks import call_tool_task
from fastmcp_tasks.client_models import ClientGetTaskResult

from clio_agent.errors import ConfigError, ToolError
from clio_agent.tools.mcp_runtime import make_mcp_client
from clio_agent.tools.mcp_task_extension import (
    backend_identity,
    persist_created_task,
)
from clio_agent.tools.mcp_task_records import (
    TERMINAL_TASK_STATES,
    TaskInputLedger,
    TaskKey,
    TaskRecordStore,
    persist_ledger,
    resolve_store,
)
from clio_agent.tools.mcp_tasks import (
    cancel_task,
    resume_task,
    send_task_get,
    send_task_update,
)

RELAY_POLL_INTERVAL_MS = 1_000
RELAY_RESULT_DELIVERY_SCHEMA = "clio-relay.mcp-result-delivery.v1"
RELAY_INLINE_LIMIT_CODE = "inline_result_limit_exceeded"
OWNER_SESSION_ID_HEADER = "X-Clio-Relay-Owner-Session-Id"
SESSION_GENERATION_ID_HEADER = "X-Clio-Relay-Session-Generation-Id"
REMOTE_MCP_ALIAS_PREFIX = "remote_"
REMOTE_MCP_CATALOG_META_KEY = "clio-relay/catalog-revision"
REMOTE_MCP_HANDLE_FIELDS = frozenset({"job_id", "state", "kind", "terminal"})
REMOTE_MCP_FOLLOW_TOOLS = frozenset({"relay_observe", "relay_wait"})

__all__ = [
    "OWNER_SESSION_ID_HEADER",
    "RELAY_POLL_INTERVAL_MS",
    "RELAY_RESULT_DELIVERY_SCHEMA",
    "SESSION_GENERATION_ID_HEADER",
    "RelayInlineResultTooLargeError",
    "RelayMcpNameMismatchError",
    "RelayPollIntervalMismatchError",
    "RelayRemoteMcpCatalog",
    "RelayRemoteMcpCatalogStaleError",
    "RelayRemoteMcpHandle",
    "RelayTaskIdentity",
    "RelayTaskJobMismatchError",
    "RelayTransportClient",
    "RelayTransportContractError",
]


class RelayTransportContractError(ToolError):
    """A relay response or caller identity violated the transport contract."""

    def __init__(self, message: str, *, reason: str, details: dict[str, Any]) -> None:
        self.reason = reason
        super().__init__(message, details={"reason": reason, **details})


class RelayTaskJobMismatchError(RelayTransportContractError):
    """The relay task id and durable job id are not identical."""

    def __init__(self, task_id: str, job_id: str) -> None:
        super().__init__(
            f"relay taskId {task_id!r} does not equal jobId {job_id!r}",
            reason="relay_task_job_id_mismatch",
            details={"task_id": task_id, "job_id": job_id},
        )


class RelayMcpNameMismatchError(RelayTransportContractError):
    """The task-management routing name differs from the task id."""

    def __init__(self, task_id: str, mcp_name: str) -> None:
        super().__init__(
            f"Mcp-Name {mcp_name!r} does not equal relay task id {task_id!r}",
            reason="relay_mcp_name_mismatch",
            details={"task_id": task_id, "mcp_name": mcp_name},
        )


class RelayPollIntervalMismatchError(RelayTransportContractError):
    """The relay advertised a task cadence other than its fixed one-second poll."""

    def __init__(self, task_id: str, observed: float | None) -> None:
        super().__init__(
            f"relay task {task_id!r} advertised pollIntervalMs={observed!r}; "
            f"expected {RELAY_POLL_INTERVAL_MS}",
            reason="relay_poll_interval_mismatch",
            details={
                "task_id": task_id,
                "poll_interval_ms": observed,
                "expected_poll_interval_ms": RELAY_POLL_INTERVAL_MS,
            },
        )


class RelayInlineResultTooLargeError(RelayTransportContractError):
    """Relay's typed 64 KiB inline-delivery failure."""

    def __init__(self, task_id: str, delivery: Mapping[str, Any]) -> None:
        self.delivery = dict(delivery)
        super().__init__(
            str(delivery.get("message") or "relay result exceeded the inline delivery limit"),
            reason=RELAY_INLINE_LIMIT_CODE,
            details={"task_id": task_id, "delivery": self.delivery},
        )


class RelayRemoteMcpCatalogStaleError(RelayTransportContractError):
    """A projected remote alias no longer belongs to the current relay catalog."""

    def __init__(self, name: str, expected: str, observed: str) -> None:
        super().__init__(
            "relay remote MCP catalog changed after local tool projection",
            reason="remote_mcp_catalog_revision_stale",
            details={
                "tool": name,
                "expected_catalog_revision": expected,
                "observed_catalog_revision": observed,
                "action": "refresh_remote_mcp_catalog",
            },
        )


@dataclass(frozen=True)
class RelayRemoteMcpCatalog:
    """One relay-advertised catalog revision and its virtual ``remote_*`` tools."""

    revision: str
    tools: Mapping[str, Any]
    follow_tools: Mapping[str, Any] = dataclass_field(default_factory=dict)


@dataclass(frozen=True)
class RelayRemoteMcpHandle:
    """Handle-first local result for one durable relay ``mcp_call`` job."""

    job_id: str
    state: str
    kind: Literal["mcp_call"]
    terminal: bool
    catalog_revision: str

    def to_wire(self) -> dict[str, Any]:
        """Return the advertised local output-schema shape."""

        return {
            "job_id": self.job_id,
            "state": self.state,
            "kind": self.kind,
            "terminal": self.terminal,
            "catalog_revision": self.catalog_revision,
        }


@dataclass(frozen=True)
class RelayTaskIdentity:
    """One relay task's durable key and redundant wire invariants."""

    key: TaskKey
    job_id: str
    mcp_name: str
    poll_interval_ms: int = RELAY_POLL_INTERVAL_MS

    @property
    def task_id(self) -> str:
        """The relay-minted SEP-2663 task id."""

        return self.key.task_id

    @classmethod
    def from_key(cls, key: TaskKey) -> "RelayTaskIdentity":
        """Rebuild the invariant fields from a persisted #1115 composite key."""

        return cls(key=key, job_id=key.task_id, mcp_name=key.task_id)


class RelayTransportClient:
    """Owner-bound relay client exposing task, timeline, and artifact operations.

    Use as an async context manager.  Task lifecycle always travels through MCP;
    ``stream_events`` and ``fetch_artifact`` use the relay HTTP API with the same
    bearer and owner-session identity headers.
    """

    def __init__(
        self,
        mcp_url: str,
        http_base_url: str,
        *,
        api_token: str | None = None,
        owner_session_id: str | None = None,
        owner_session_generation_id: str | None = None,
        session_id: str | None = None,
        store: TaskRecordStore | None = None,
        request_timeout_seconds: float = 30.0,
    ) -> None:
        token = api_token if api_token is not None else os.getenv("CLIO_RELAY_API_TOKEN")
        if not token:
            raise ConfigError("CLIO_RELAY_API_TOKEN is required for the relay transport")
        if (owner_session_id is None) != (owner_session_generation_id is None):
            raise ConfigError(
                "relay owner_session_id and owner_session_generation_id must be supplied together"
            )
        if request_timeout_seconds <= 0:
            raise ConfigError("relay request_timeout_seconds must be positive")
        self._mcp_url = mcp_url
        self._http_base_url = http_base_url.rstrip("/")
        self._session_id = session_id if session_id is not None else owner_session_id
        self._store = store
        self._timeout = request_timeout_seconds
        self._headers = {"Authorization": f"Bearer {token}"}
        if owner_session_id is not None and owner_session_generation_id is not None:
            self._headers.update(
                {
                    OWNER_SESSION_ID_HEADER: owner_session_id,
                    SESSION_GENERATION_ID_HEADER: owner_session_generation_id,
                }
            )
        self._mcp_client: Any | None = None
        self._http_client: httpx.AsyncClient | None = None

    async def __aenter__(self) -> "RelayTransportClient":
        """Open both authenticated doors."""

        if self._mcp_client is not None or self._http_client is not None:
            raise RuntimeError("relay transport client is already open")
        # Credential attach stays with the factory owners (#1118 single-owner
        # guard): the runtime-dict spec routes headers through transport_from_spec.
        mcp_client = make_mcp_client(
            {"transport": "http", "url": self._mcp_url, "headers": dict(self._headers)}
        )
        await mcp_client.__aenter__()
        self._mcp_client = mcp_client
        self._http_client = httpx.AsyncClient(
            base_url=self._http_base_url,
            headers=dict(self._headers),
            timeout=self._timeout,
        )
        return self

    async def __aexit__(self, exc_type: Any, exc: Any, tb: Any) -> None:
        """Close both doors without suppressing the caller's exception."""

        http_client = self._http_client
        mcp_client = self._mcp_client
        self._http_client = None
        self._mcp_client = None
        if http_client is not None:
            await http_client.aclose()
        if mcp_client is not None:
            await mcp_client.__aexit__(exc_type, exc, tb)

    async def submit(
        self,
        tool_name: str,
        arguments: Mapping[str, Any] | None = None,
        *,
        idempotency_key: str | None = None,
        timeout_seconds: float | None = None,
    ) -> RelayTaskIdentity:
        """Submit a relay task, then durably record its handle before returning.

        ``idempotency_key`` is passed only through the relay tool's documented
        argument.  A conflicting value already present in ``arguments`` is rejected
        locally so one call cannot carry two create identities.
        """

        client = self._require_mcp_client()
        payload = dict(arguments or {})
        existing_key = payload.get("idempotency_key")
        if idempotency_key is not None:
            if existing_key is not None and existing_key != idempotency_key:
                raise RelayTransportContractError(
                    "relay submission contains conflicting idempotency keys",
                    reason="relay_idempotency_key_mismatch",
                    details={
                        "argument_idempotency_key": existing_key,
                        "client_idempotency_key": idempotency_key,
                    },
                )
            payload["idempotency_key"] = idempotency_key

        handle = await call_tool_task(
            client,
            tool_name,
            payload,
            timeout=timeout_seconds,
        )
        create_result = handle.create_result
        self._require_poll_interval(handle.task_id, create_result.poll_interval_ms)
        backend = backend_identity(client.transport)
        key = TaskKey(
            server_id=backend.server_id,
            session_id=self._session_id,
            task_id=handle.task_id,
        )
        await persist_created_task(
            client.session,
            key,
            create_result,
            identity=backend,
            tool_name=tool_name,
            store=self._record_store(),
        )
        return RelayTaskIdentity.from_key(key)

    async def discover_remote_mcp(self) -> RelayRemoteMcpCatalog:
        """Read and validate the relay-owned catalog of virtual ``remote_*`` tools."""

        client = self._require_mcp_client()
        tools: list[Any] = []
        cursor: str | None = None
        seen_cursors: set[str] = set()
        revision: str | None = None
        for _page_number in range(250):
            page = await client.list_tools_mcp(cursor=cursor)
            page_meta = page.meta if isinstance(page.meta, Mapping) else {}
            page_revision = page_meta.get("clio-relay/remote-mcp-catalog-revision")
            if not isinstance(page_revision, str) or not page_revision:
                tool_revisions = {
                    tool.meta.get(REMOTE_MCP_CATALOG_META_KEY)
                    for tool in page.tools
                    if tool.name.startswith(REMOTE_MCP_ALIAS_PREFIX)
                    and isinstance(tool.meta, Mapping)
                    and isinstance(tool.meta.get(REMOTE_MCP_CATALOG_META_KEY), str)
                }
                if len(tool_revisions) == 1:
                    page_revision = tool_revisions.pop()
            if not isinstance(page_revision, str) or not page_revision:
                raise RelayTransportContractError(
                    "relay tools/list omitted its remote MCP catalog revision",
                    reason="remote_mcp_catalog_revision_missing",
                    details={},
                )
            if revision is not None and page_revision != revision:
                raise RelayTransportContractError(
                    "relay remote MCP catalog revision changed during pagination",
                    reason="remote_mcp_catalog_revision_changed_during_list",
                    details={"first_revision": revision, "observed_revision": page_revision},
                )
            revision = page_revision
            tools.extend(page.tools)
            cursor = page.next_cursor
            if not cursor:
                break
            if cursor in seen_cursors:
                raise RelayTransportContractError(
                    "relay tools/list repeated a remote MCP catalog cursor",
                    reason="remote_mcp_catalog_cursor_repeated",
                    details={"cursor": cursor},
                )
            seen_cursors.add(cursor)
        else:
            raise RelayTransportContractError(
                "relay remote MCP catalog exceeded the pagination bound",
                reason="remote_mcp_catalog_page_limit_exceeded",
                details={"max_pages": 250},
            )

        assert revision is not None
        projected: dict[str, Any] = {}
        follow_tools: dict[str, Any] = {}
        for tool in tools:
            if tool.name in REMOTE_MCP_FOLLOW_TOOLS:
                follow_tools[tool.name] = tool
                continue
            if not tool.name.startswith(REMOTE_MCP_ALIAS_PREFIX):
                continue
            self._validate_remote_mcp_definition(tool, revision)
            projected[tool.name] = tool
        return RelayRemoteMcpCatalog(
            revision=revision,
            tools=projected,
            follow_tools=follow_tools,
        )

    async def submit_remote_mcp(
        self,
        name: str,
        arguments: Mapping[str, Any],
        *,
        catalog_revision: str,
    ) -> RelayRemoteMcpHandle:
        """Bind one virtual call to its listed revision and return a job handle."""

        current = await self.discover_remote_mcp()
        if current.revision != catalog_revision or name not in current.tools:
            raise RelayRemoteMcpCatalogStaleError(
                name,
                catalog_revision,
                current.revision,
            )
        identity = await self.submit(name, arguments)
        return RelayRemoteMcpHandle(
            job_id=identity.job_id,
            state="queued",
            kind="mcp_call",
            terminal=False,
            catalog_revision=catalog_revision,
        )

    async def call_relay_tool(
        self,
        name: str,
        arguments: Mapping[str, Any],
    ) -> Any:
        """Call one bounded relay follow tool and retain its typed MCP result."""

        if name not in REMOTE_MCP_FOLLOW_TOOLS:
            raise ValueError(f"unsupported relay federation follow tool: {name!r}")
        return await self._require_mcp_client().call_tool_mcp(name, dict(arguments))

    async def poll(self, task: RelayTaskIdentity) -> ClientGetTaskResult:
        """Perform one named ``tasks/get`` and persist the observed transition."""

        self._validate_identity(task)
        store = self._record_store()
        record = store.get(task.key)
        if record is None:
            raise RelayTransportContractError(
                f"relay task {task.task_id!r} has no persisted #1115 record",
                reason="relay_task_record_missing",
                details=task.key.to_wire(),
            )
        current = await send_task_get(
            self._require_mcp_client().session,
            task.task_id,
            self._timeout,
        )
        self._require_poll_interval(task.task_id, current.poll_interval_ms)
        if current.task_id != task.task_id:
            raise RelayTaskJobMismatchError(task.task_id, current.task_id)
        self._validate_result(task, current)
        if current.status in TERMINAL_TASK_STATES:
            store.drop(task.key)
        else:
            store.put(replace(record, status=current.status))
        return current

    async def wait(
        self,
        task: RelayTaskIdentity,
        *,
        timeout_seconds: float | None = None,
    ) -> ClientGetTaskResult:
        """Drive a persisted task to terminal through #1115's leased poll loop."""

        self._validate_identity(task)
        final = await resume_task(
            self._require_mcp_client().session,
            task.key,
            timeout_seconds=timeout_seconds,
            store=self._record_store(),
        )
        self._require_poll_interval(task.task_id, final.poll_interval_ms)
        self._validate_result(task, final)
        return final

    async def resume(
        self,
        key: TaskKey,
        *,
        timeout_seconds: float | None = None,
    ) -> ClientGetTaskResult:
        """Rebuild identity from a durable key and resume it on this fresh client."""

        return await self.wait(
            RelayTaskIdentity.from_key(key),
            timeout_seconds=timeout_seconds,
        )

    async def cancel(self, task: RelayTaskIdentity) -> Any:
        """Acknowledge cooperative cancellation while retaining reconnect state.

        Canonical cancelled state is eventually consistent and arrives through a
        later tasks/get. The durable row therefore remains until poll or wait
        observes a terminal state.
        """

        self._validate_identity(task)
        store = self._record_store()
        record = store.get(task.key)
        if record is None:
            raise RelayTransportContractError(
                f"relay task {task.task_id!r} has no persisted #1115 record",
                reason="relay_task_record_missing",
                details=task.key.to_wire(),
            )
        ack = await cancel_task(
            self._require_mcp_client().session,
            task.key,
            store=store,
        )
        store.put(replace(record, cancel_requested=True))
        return ack

    async def message(self, task: RelayTaskIdentity, text: str) -> None:
        """Deliver one agent message through relay's durable tasks/update round.

        The exact accepted elicitation payload is written through #1115's shared
        input-answer ledger before transmission. A retry therefore re-sends the
        same bytes; a conflicting second payload is refused instead of replacing
        the answer relay has already consumed.
        """

        self._validate_identity(task)
        message = str(text or "")
        if not message.strip():
            raise RelayTransportContractError(
                "relay agent message must be non-empty",
                reason="agent_message_empty",
                details={"task_id": task.task_id},
            )
        store = self._record_store()
        record = store.get(task.key)
        if record is None:
            raise RelayTransportContractError(
                f"relay task {task.task_id!r} has no persisted #1115 record",
                reason="relay_task_record_missing",
                details=task.key.to_wire(),
            )
        payload = {"action": "accept", "content": {"message": message}}
        ledger = TaskInputLedger.from_record(record)
        existing = ledger.answer("agent_message")
        if existing is not None and existing.payload != payload:
            raise RelayTransportContractError(
                "relay task already captured a different agent message",
                reason="agent_message_already_captured",
                details={"task_id": task.task_id, "input_key": "agent_message"},
            )
        if existing is None:
            ledger.capture("agent_message", payload)
            persist_ledger(store, task.key, ledger)
        await send_task_update(
            self._require_mcp_client().session,
            task.task_id,
            {"agent_message": payload},
            self._timeout,
        )
        ledger.mark_delivered(["agent_message"])
        persist_ledger(store, task.key, ledger)

    async def stream_events(
        self,
        task: RelayTaskIdentity,
        *,
        cursor: int = 1,
        limit: int = 100,
        poll_seconds: float = 1.0,
    ) -> AsyncIterator[dict[str, Any]]:
        """Yield individual live timeline events from relay's SSE door.

        The stream is observation-only.  Task terminal state continues to come only
        from ``tasks/get`` so reconnect and leasing retain one authoritative driver.
        """

        self._validate_identity(task)
        if cursor < 1 or limit < 1 or poll_seconds <= 0:
            raise ValueError("relay event cursor, limit, and poll_seconds must be positive")
        path = f"/tasks/{quote(task.task_id, safe='')}/events/sse"
        params = {"cursor": cursor, "limit": limit, "poll_seconds": poll_seconds}
        async with self._require_http_client().stream("GET", path, params=params) as response:
            response.raise_for_status()
            event_name = ""
            data_lines: list[str] = []
            async for line in response.aiter_lines():
                if line:
                    if line.startswith("event:"):
                        event_name = line.partition(":")[2].strip()
                    elif line.startswith("data:"):
                        data_lines.append(line.partition(":")[2].lstrip())
                    continue
                if event_name == "task_events" and data_lines:
                    payload = self._decode_sse_payload(task, "\n".join(data_lines))
                    for event in payload:
                        yield event
                event_name = ""
                data_lines = []
            if event_name == "task_events" and data_lines:
                for event in self._decode_sse_payload(task, "\n".join(data_lines)):
                    yield event

    async def fetch_artifact(self, artifact_id: str) -> bytes:
        """Fetch and decode an out-of-band relay artifact content envelope."""

        path = f"/artifacts/{quote(artifact_id, safe='')}/content"
        response = await self._require_http_client().get(path)
        response.raise_for_status()
        try:
            payload = response.json()
        except json.JSONDecodeError as exc:
            raise RelayTransportContractError(
                "relay artifact response is not JSON",
                reason="relay_artifact_response_invalid",
                details={"artifact_id": artifact_id},
            ) from exc
        if not isinstance(payload, Mapping):
            raise RelayTransportContractError(
                "relay artifact response is not an object",
                reason="relay_artifact_response_invalid",
                details={"artifact_id": artifact_id},
            )
        artifact = payload.get("artifact")
        observed_id = artifact.get("artifact_id") if isinstance(artifact, Mapping) else None
        if observed_id != artifact_id:
            raise RelayTransportContractError(
                "relay artifact content identity does not match the request",
                reason="relay_artifact_id_mismatch",
                details={"artifact_id": artifact_id, "observed_artifact_id": observed_id},
            )
        if payload.get("encoding") != "base64" or not isinstance(payload.get("data"), str):
            raise RelayTransportContractError(
                "relay artifact content envelope is not base64",
                reason="relay_artifact_encoding_invalid",
                details={"artifact_id": artifact_id},
            )
        try:
            return base64.b64decode(payload["data"], validate=True)
        except (binascii.Error, ValueError) as exc:
            raise RelayTransportContractError(
                "relay artifact content contains invalid base64",
                reason="relay_artifact_encoding_invalid",
                details={"artifact_id": artifact_id},
            ) from exc

    def _record_store(self) -> TaskRecordStore:
        """Resolve the explicit store or #1115's installed durable home."""

        return resolve_store(self._store)

    @staticmethod
    def _validate_remote_mcp_definition(tool: Any, revision: str) -> None:
        """Reject a dynamic relay definition that is not handle-first or revision-bound."""

        meta = tool.meta if isinstance(tool.meta, Mapping) else {}
        if meta.get(REMOTE_MCP_CATALOG_META_KEY) != revision:
            raise RelayTransportContractError(
                "relay remote MCP tool revision does not match tools/list",
                reason="remote_mcp_tool_revision_mismatch",
                details={
                    "tool": tool.name,
                    "catalog_revision": revision,
                    "tool_revision": meta.get(REMOTE_MCP_CATALOG_META_KEY),
                },
            )
        output_schema = tool.output_schema
        properties = output_schema.get("properties") if isinstance(output_schema, Mapping) else None
        required = output_schema.get("required") if isinstance(output_schema, Mapping) else None
        if (
            not isinstance(properties, Mapping)
            or not isinstance(required, list)
            or not REMOTE_MCP_HANDLE_FIELDS.issubset(required)
            or properties.get("kind") != {"type": "string", "const": "mcp_call"}
        ):
            raise RelayTransportContractError(
                "relay remote MCP tool does not advertise the durable job handle output schema",
                reason="remote_mcp_handle_output_schema_invalid",
                details={"tool": tool.name},
            )

    def _require_mcp_client(self) -> Any:
        """Return the open FastMCP client."""

        if self._mcp_client is None:
            raise RuntimeError("relay transport client is not open")
        return self._mcp_client

    def _require_http_client(self) -> httpx.AsyncClient:
        """Return the open HTTP client."""

        if self._http_client is None:
            raise RuntimeError("relay transport client is not open")
        return self._http_client

    @staticmethod
    def _validate_identity(task: RelayTaskIdentity) -> None:
        """Reject redundant identity mismatches before touching either door."""

        if task.job_id != task.task_id:
            raise RelayTaskJobMismatchError(task.task_id, task.job_id)
        if task.mcp_name != task.task_id:
            raise RelayMcpNameMismatchError(task.task_id, task.mcp_name)
        RelayTransportClient._require_poll_interval(task.task_id, task.poll_interval_ms)

    @staticmethod
    def _require_poll_interval(task_id: str, observed: float | None) -> None:
        """Enforce relay's fixed poll-only one-second cadence."""

        if observed != RELAY_POLL_INTERVAL_MS:
            raise RelayPollIntervalMismatchError(task_id, observed)

    @staticmethod
    def _validate_result(task: RelayTaskIdentity, current: ClientGetTaskResult) -> None:
        """Reject typed delivery failures and any task/job identity drift."""

        if current.result is None:
            return
        for item in _walk_result_objects(current.result):
            delivery = item.get("delivery")
            if (
                isinstance(delivery, Mapping)
                and delivery.get("schema_version") == RELAY_RESULT_DELIVERY_SCHEMA
                and delivery.get("status") == "failed"
                and delivery.get("code") == RELAY_INLINE_LIMIT_CODE
            ):
                raise RelayInlineResultTooLargeError(task.task_id, delivery)
            for field in ("job_id", "jobId"):
                job_id = item.get(field)
                if isinstance(job_id, str) and job_id != task.task_id:
                    raise RelayTaskJobMismatchError(task.task_id, job_id)

    @staticmethod
    def _decode_sse_payload(task: RelayTaskIdentity, encoded: str) -> list[dict[str, Any]]:
        """Decode one relay ``task_events`` SSE block and validate its identity."""

        try:
            payload = json.loads(encoded)
        except json.JSONDecodeError as exc:
            raise RelayTransportContractError(
                "relay task event stream emitted invalid JSON",
                reason="relay_task_event_invalid",
                details={"task_id": task.task_id},
            ) from exc
        observed_task_id = payload.get("task_id") if isinstance(payload, Mapping) else ""
        if observed_task_id != task.task_id:
            raise RelayTaskJobMismatchError(task.task_id, str(observed_task_id))
        events = payload.get("events")
        if not isinstance(events, list):
            raise RelayTransportContractError(
                "relay task event stream omitted its events list",
                reason="relay_task_event_invalid",
                details={"task_id": task.task_id},
            )
        decoded: list[dict[str, Any]] = []
        for event in events:
            if not isinstance(event, Mapping) or event.get("task_id") != task.task_id:
                observed = event.get("task_id") if isinstance(event, Mapping) else ""
                raise RelayTaskJobMismatchError(task.task_id, str(observed))
            decoded.append(dict(event))
        return decoded


def _walk_result_objects(value: Any) -> Iterator[Mapping[str, Any]]:
    """Walk the bounded JSON-shaped task result, including JSON text content."""

    stack = [value]
    seen = 0
    while stack:
        current = stack.pop()
        seen += 1
        if seen > 100_000:
            raise RelayTransportContractError(
                "relay task result exceeds the client validation node bound",
                reason="relay_task_result_too_complex",
                details={"max_nodes": 100_000},
            )
        if isinstance(current, Mapping):
            yield current
            stack.extend(current.values())
        elif isinstance(current, (list, tuple)):
            stack.extend(current)
        elif isinstance(current, str) and current.lstrip().startswith(("{", "[")):
            try:
                stack.append(json.loads(current))
            except json.JSONDecodeError:
                continue
