"""Local task acceptance and validation of the backend's actual terminal result."""

from __future__ import annotations

import json
from dataclasses import replace
from typing import Any

import mcp_types
from fastmcp import Client
from fastmcp.client.client import CallToolResult
from fastmcp_tasks.client import _inlined_call_tool_result
from jsonschema import ValidationError, validate
from mcp.client.extension import ClaimContext


class AcceptedTaskResult(mcp_types.CallToolResult):
    """An application-authored receipt, distinct from a backend terminal payload."""


class TaskResultValidationError(ValueError):
    """A settled backend result violated the original declared output contract."""


class TaskAwareClient(Client):
    """Parse local acceptance separately while preserving backend result validation."""

    async def _resolve_claimed_result(
        self, name: str, result: mcp_types.Result, read_timeout_seconds: float | None
    ) -> mcp_types.CallToolResult:
        claim = self._claim_by_model[type(result)]
        final = await claim.resolve(
            result,
            ClaimContext(
                session=self.session, tool_name=name, read_timeout_seconds=read_timeout_seconds
            ),
        )
        if not isinstance(final, AcceptedTaskResult) and not final.is_error:
            await self.session.validate_tool_result(name, final)
        return final

    async def _parse_call_tool_result(
        self, name: str, result: mcp_types.CallToolResult, raise_on_error: bool = False
    ) -> CallToolResult:
        if isinstance(result, AcceptedTaskResult):
            return CallToolResult(
                content=result.content,
                structured_content=result.structured_content,
                meta=result.meta,
                data=result.structured_content,
                is_error=False,
            )
        return await super()._parse_call_tool_result(name, result, raise_on_error)


def accepted_result(receipt: dict[str, Any]) -> AcceptedTaskResult:
    """Validate the application receipt before returning it through the SDK."""
    if (
        receipt.get("accepted") is not True
        or receipt.get("kind") != "MCP"
        or not str(receipt.get("handle", "")).startswith("task_")
    ):
        raise ValueError("Invalid CLIO task acceptance receipt")
    return AcceptedTaskResult(
        content=[mcp_types.TextContent(type="text", text=json.dumps(receipt))],
        structured_content=receipt,
    )


def validate_terminal(record: Any, current: Any) -> None:
    """Validate the original backend schema before successful completion is persisted."""
    if current.status != "completed":
        return
    schema = record.backend.get("output_schema")
    result = _inlined_call_tool_result(current.result)
    if result.is_error or schema is None:
        return
    try:
        if result.structured_content is None:
            raise TaskResultValidationError("Backend omitted its declared structured result")
        validate(result.structured_content, schema)
    except ValidationError as exc:
        raise TaskResultValidationError(
            f"Backend result violates its declared schema: {exc.message}"
        ) from exc


def record_invalid_result(store: Any, key: Any, error: TaskResultValidationError) -> None:
    """Preserve raw settlement while publishing a terminal contract failure honestly."""
    row = store.get(key)
    if row is not None:
        store.put(
            replace(
                row,
                status="completed",
                effective_status="failed",
                effective_status_reason=str(error),
                result={"error": str(error)},
                notify_pending=not bool(row.consumed_at),
            )
        )
