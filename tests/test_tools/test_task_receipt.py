"""Validate local acceptance and terminal backend envelopes independently."""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from clio_agent.tools.mcp_task_records import InMemoryTaskRecordStore, TaskKey, TaskRecord
from clio_agent.tools.task_receipt import (
    TaskResultValidationError,
    accepted_result,
    record_invalid_result,
    validate_terminal,
)


def test_acceptance_never_validates_as_backend_payload_and_invalid_result_fails() -> None:
    """A receipt is local; successful settlement still requires the original backend contract."""
    key = TaskKey("actual-server", "owned-session", "actual-task", "native-session")
    record = TaskRecord(
        key=key,
        handle="task_owned",
        kind="MCP",
        backend={
            "output_schema": {
                "type": "object",
                "properties": {"value": {"type": "integer"}},
                "required": ["value"],
                "additionalProperties": False,
            },
        },
    )
    receipt = accepted_result({"accepted": True, "kind": "MCP", "handle": record.handle})
    assert receipt.structured_content["handle"] == record.handle
    store = InMemoryTaskRecordStore()
    store.put(record)
    current = SimpleNamespace(
        status="completed",
        result={
            "content": [],
            "structuredContent": {"value": "violates backend contract"},
            "isError": False,
        },
    )
    with pytest.raises(TaskResultValidationError, match="declared schema") as caught:
        validate_terminal(record, current)
    record_invalid_result(store, key, caught.value)
    failed = store.get(key)
    assert failed.status == "completed" and failed.effective_status == "failed"
    assert failed.result["error"] and failed.notify_pending
    current.result["structuredContent"] = {"value": 7}
    validate_terminal(record, current)


def test_missing_declared_terminal_payload_is_a_contract_failure() -> None:
    """Missing structured output cannot silently turn into a successful completion."""
    record = TaskRecord(
        key=TaskKey("server", "session", "task"),
        backend={
            "output_schema": {"type": "object"},
        },
    )
    current = SimpleNamespace(status="completed", result={"content": [], "isError": False})
    with pytest.raises(TaskResultValidationError, match="omitted"):
        validate_terminal(record, current)
