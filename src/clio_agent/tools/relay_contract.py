"""Typed validation helpers for the two-door relay transport."""

from __future__ import annotations

import json
from collections.abc import Iterator, Mapping
from typing import Any, NoReturn

from fastmcp_tasks.client_models import ClientGetTaskResult

from clio_agent.errors import ToolError

RELAY_POLL_INTERVAL_MS = 1_000
RELAY_RESULT_DELIVERY_SCHEMA = "clio-relay.mcp-result-delivery.v1"
RELAY_INLINE_LIMIT_CODE = "inline_result_limit_exceeded"
RELAY_EVENT_NEXT_CURSOR_FIELD = "_relay_next_cursor"


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


async def validate_submit_arguments(
    client: Any,
    schemas: dict[str, Mapping[str, Any]] | None,
    tool_name: str,
    payload: Mapping[str, Any],
) -> dict[str, Mapping[str, Any]]:
    """Reject missing or forbidden top-level keys using the discovered schema."""

    if schemas is None:
        schemas = {}
        cursor: str | None = None
        seen: set[str] = set()
        for _page in range(250):
            page = await client.list_tools_mcp(cursor=cursor)
            for tool in page.tools:
                schema = getattr(tool, "input_schema", None)
                if isinstance(schema, Mapping):
                    schemas[str(tool.name)] = schema
            cursor = page.next_cursor
            if not cursor:
                break
            if cursor in seen:
                raise RelayTransportContractError(
                    "relay tools/list repeated a validation cursor",
                    reason="relay_tool_catalog_cursor_repeated",
                    details={"cursor": cursor},
                )
            seen.add(cursor)
        else:
            raise RelayTransportContractError(
                "relay tool catalog exceeded the validation pagination bound",
                reason="relay_tool_catalog_page_limit_exceeded",
                details={"max_pages": 250},
            )

    schema = schemas.get(tool_name)
    if schema is None:
        raise RelayTransportContractError(
            f"relay tool {tool_name!r} was not present in the discovered catalog",
            reason="relay_tool_not_found",
            details={"tool": tool_name},
        )
    required = {str(key) for key in schema.get("required", ()) if isinstance(key, str)}
    properties_obj = schema.get("properties", {})
    properties = set(properties_obj) if isinstance(properties_obj, Mapping) else set()
    missing = sorted(required - set(payload))
    unknown = (
        sorted(set(payload) - properties) if schema.get("additionalProperties") is False else []
    )
    if missing or unknown:
        raise RelayTransportContractError(
            f"relay arguments for {tool_name!r} do not match its discovered inputSchema",
            reason="relay_arguments_invalid",
            details={
                "tool": tool_name,
                "missing_keys": missing,
                "unknown_keys": unknown,
            },
        )
    return schemas


def raise_inline_submission(tool_name: str, raw: Any) -> NoReturn:
    """Turn a non-task inline response into a typed error with relay detail."""

    content = getattr(raw, "content", ()) or ()
    text_parts = [
        str(getattr(item, "text", "")) for item in content if str(getattr(item, "text", "")).strip()
    ]
    relay_error = "\n".join(text_parts).strip() or str(raw)
    structured = getattr(raw, "structured_content", None)
    is_error = bool(getattr(raw, "is_error", False))
    reason = "relay_call_rejected_inline" if is_error else "relay_call_returned_inline"
    raise RelayTransportContractError(
        relay_error,
        reason=reason,
        details={
            "tool": tool_name,
            "relay_error": relay_error,
            "structured_content": structured,
        },
    )


def validate_result(task_id: str, current: ClientGetTaskResult) -> None:
    """Reject typed delivery failures and top-level task/job identity drift."""

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
            raise RelayInlineResultTooLargeError(task_id, delivery)
    for envelope in _identity_result_envelopes(current.result):
        for field in ("job_id", "jobId"):
            job_id = envelope.get(field)
            if isinstance(job_id, str) and job_id != task_id:
                raise RelayTaskJobMismatchError(task_id, job_id)


def decode_sse_payload(task_id: str, encoded: str) -> list[dict[str, Any]]:
    """Decode one relay ``task_events`` SSE block and validate its identity."""

    try:
        payload = json.loads(encoded)
    except json.JSONDecodeError as exc:
        raise RelayTransportContractError(
            "relay task event stream emitted invalid JSON",
            reason="relay_task_event_invalid",
            details={"task_id": task_id},
        ) from exc
    observed_task_id = payload.get("task_id") if isinstance(payload, Mapping) else ""
    if observed_task_id != task_id:
        raise RelayTaskJobMismatchError(task_id, str(observed_task_id))
    events = payload.get("events")
    if not isinstance(events, list):
        raise RelayTransportContractError(
            "relay task event stream omitted its events list",
            reason="relay_task_event_invalid",
            details={"task_id": task_id},
        )
    next_cursor = payload.get("next_cursor")
    if isinstance(next_cursor, bool) or not isinstance(next_cursor, int) or next_cursor < 1:
        raise RelayTransportContractError(
            "relay task event stream omitted a valid next_cursor",
            reason="relay_task_event_invalid",
            details={"task_id": task_id},
        )
    decoded: list[dict[str, Any]] = []
    for event in events:
        if not isinstance(event, Mapping) or event.get("task_id") != task_id:
            observed = event.get("task_id") if isinstance(event, Mapping) else ""
            raise RelayTaskJobMismatchError(task_id, str(observed))
        decoded.append({**dict(event), RELAY_EVENT_NEXT_CURSOR_FIELD: next_cursor})
    return decoded


def _identity_result_envelopes(value: Any) -> Iterator[Mapping[str, Any]]:
    """Yield only delivery boundaries allowed to assert relay job identity."""

    current = value
    if isinstance(current, str) and current.lstrip().startswith("{"):
        try:
            current = json.loads(current)
        except json.JSONDecodeError:
            return
    if not isinstance(current, Mapping):
        return
    yield current
    for key in ("structuredContent", "structured_content", "task_result"):
        nested = current.get(key)
        if isinstance(nested, Mapping):
            yield nested


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
