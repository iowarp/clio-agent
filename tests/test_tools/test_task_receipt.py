"""Acceptance receipts and captured backend contracts, without mutable legacy records."""

from __future__ import annotations

import pytest
from fastmcp_tasks.client_models import ClientGetTaskResult

from clio_agent.tools.task_receipt import (
    FOLLOW_HANDLE,
    UNCERTAIN_HANDLE,
    AcceptedTaskResult,
    TaskResultValidationError,
    accepted_result,
    validate_terminal,
)


def terminal(result: dict[str, object]) -> ClientGetTaskResult:
    """Decode the actual SDK terminal envelope used by production observers."""
    return ClientGetTaskResult.model_validate(
        {
            "resultType": "complete",
            "taskId": "native",
            "status": "completed",
            "createdAt": "2026-10-14T00:00:00Z",
            "lastUpdatedAt": "2026-10-14T00:00:01Z",
            "result": result,
        }
    )


def test_acceptance_never_validates_as_backend_payload_and_invalid_result_fails() -> None:
    """Local acceptance cannot bypass validation of the original backend schema."""
    schema = {
        "type": "object",
        "properties": {"value": {"type": "integer"}},
        "required": ["value"],
        "additionalProperties": False,
    }
    receipt = accepted_result({"accepted": True, "kind": "MCP", "handle": "task_owned"})
    assert isinstance(receipt, AcceptedTaskResult)
    assert receipt.structured_content == {
        "accepted": True,
        "kind": "MCP",
        "handle": "task_owned",
        "next": FOLLOW_HANDLE,
    }
    current = terminal(
        {
            "content": [],
            "structuredContent": {"value": "violates backend contract"},
            "isError": False,
        }
    )
    with pytest.raises(TaskResultValidationError, match="declared schema"):
        validate_terminal(schema, current)
    assert current.result is not None
    assert current.result["structuredContent"] == {"value": "violates backend contract"}
    validate_terminal(
        schema, terminal({"content": [], "structuredContent": {"value": 7}, "isError": False})
    )
    with pytest.raises(TaskResultValidationError, match="declared schema"):
        validate_terminal(schema, terminal(receipt.model_dump(mode="json", by_alias=True)))


def test_a_receipt_tells_the_caller_how_to_follow_its_handle() -> None:
    """Found live: a model given only ``{kind, status: running}`` redid the work by hand
    instead of waiting on the task. The receipt now says what to do with the handle."""
    running = accepted_result(
        {"handle": "task_run", "kind": "MCP", "accepted": True, "status": "running"}
    )
    assert running.structured_content is not None
    assert running.structured_content["next"] == FOLLOW_HANDLE
    assert "wait_tasks" in running.content[0].text  # type: ignore[union-attr]

    unsure = accepted_result(
        {
            "handle": "task_unsure",
            "kind": "MCP",
            "accepted": None,
            "acceptance": "uncertain",
            "retry_safe": False,
        }
    )
    assert unsure.structured_content is not None
    assert unsure.structured_content["next"] == UNCERTAIN_HANDLE

    stored = accepted_result(dict(running.structured_content))
    assert stored.structured_content == running.structured_content


def test_missing_declared_terminal_payload_is_a_contract_failure() -> None:
    """A completed task with no declared output is never silently accepted."""
    with pytest.raises(TaskResultValidationError, match="omitted"):
        validate_terminal({"type": "object"}, terminal({"content": [], "isError": False}))
