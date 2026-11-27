"""Local task acceptance and validation of the backend's actual terminal result."""

from __future__ import annotations

import json
from typing import Any

import mcp_types
from fastmcp_tasks.client import _inlined_call_tool_result
from fastmcp_tasks.client_models import ClientGetTaskResult
from jsonschema import Draft202012Validator, SchemaError, ValidationError
from jsonschema.validators import validator_for
from pydantic import ValidationError as ModelValidationError
from referencing import Registry
from referencing.exceptions import Unresolvable


class AcceptedTaskResult(mcp_types.CallToolResult):
    """An application-authored receipt, distinct from a backend terminal payload."""


class TaskResultValidationError(ValueError):
    """A settled backend result violated the original declared output contract."""


#: What a caller does with an accepted handle, carried in the receipt itself so a model
#: that skims the tool description still follows the task instead of redoing its work.
FOLLOW_HANDLE = (
    "The work continues as a task. Keep this handle: call wait_tasks with it to block until "
    "it settles, then get_task_result for its result (or query_tasks to check status). The "
    "result also arrives at your next iteration. Do not redo the work another way."
)
UNCERTAIN_HANDLE = (
    "Acceptance is unconfirmed and a retry may duplicate the work. Call query_tasks with "
    "this handle to learn its state before doing anything else."
)


def accepted_result(receipt: dict[str, Any]) -> AcceptedTaskResult:
    """Validate the application receipt before returning it through the SDK."""
    if (
        (
            receipt.get("accepted") is not True
            and not (
                receipt.get("accepted") is None
                and receipt.get("acceptance") == "uncertain"
                and receipt.get("retry_safe") is False
            )
        )
        or receipt.get("kind") not in {"MCP", "Shell", "Download", "Indexing", "Code"}
        or not str(receipt.get("handle", "")).startswith("task_")
    ):
        raise ValueError("Invalid CLIO task acceptance receipt")
    if "next" not in receipt:
        followed = receipt.get("accepted") is True and receipt.get("retry_safe") is not False
        receipt = {**receipt, "next": FOLLOW_HANDLE if followed else UNCERTAIN_HANDLE}
    return AcceptedTaskResult(
        content=[mcp_types.TextContent(type="text", text=json.dumps(receipt))],
        structured_content=receipt,
    )


def validate_terminal(schema: dict[str, Any] | None, current: ClientGetTaskResult) -> None:
    """Validate the original backend schema before successful completion is persisted."""
    if current.status != "completed":
        return
    if not isinstance(current.result, dict):
        raise TaskResultValidationError("Completed backend task omitted its raw result")
    try:
        result = _inlined_call_tool_result(current.result)
    except (ModelValidationError, TypeError) as exc:
        raise TaskResultValidationError("Backend returned a malformed MCP tool result") from exc
    if result.is_error or schema is None:
        return
    try:
        if result.structured_content is None:
            raise TaskResultValidationError("Backend omitted its declared structured result")
        # Backend schemas can use local definitions, but validation must never
        # fetch arbitrary external references using the application's credentials.
        declared = schema.get("$schema")
        if "$schema" in schema and not isinstance(declared, str):
            raise TaskResultValidationError("Captured backend schema has an invalid dialect URI")
        validator = validator_for(schema, default=Draft202012Validator)
        if isinstance(declared, str):
            canonical = validator.META_SCHEMA.get("$id", validator.META_SCHEMA.get("id", ""))
            if declared.removesuffix("#") != str(canonical).removesuffix("#"):
                raise TaskResultValidationError(
                    "Captured backend schema declares an unsupported dialect"
                )
        validator.check_schema(schema)
        validator(schema, registry=Registry()).validate(result.structured_content)
    except ValidationError as exc:
        raise TaskResultValidationError(
            f"Backend result violates its declared schema: {exc.message}"
        ) from exc
    except Unresolvable as exc:
        raise TaskResultValidationError(
            "Backend schema contains an unresolved external reference"
        ) from exc
    except SchemaError as exc:
        raise TaskResultValidationError("Captured backend output schema is invalid") from exc
