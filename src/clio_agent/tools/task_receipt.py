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
