"""Read views joining a submission invocation to its own durable execution task.

The transcript keeps the original RPC observation. Execution status and result
come from Core's task owner, without consuming its mail or changing model input.
"""

from __future__ import annotations

import logging
from collections.abc import Mapping, Sequence
from datetime import datetime
from typing import Any

from fastmcp_tasks.client import _inlined_call_tool_result
from fastmcp_tasks.client_models import ClientGetTaskResult
from pydantic import ValidationError

from clio_agent.arc.task_types import TaskSnapshot
from clio_agent.gact.evidence import _bounded_tool_call_result
from clio_agent.gact.mcp_apps import call_tool_result_to_observer
from clio_agent.gact.task_access import TaskAccess, TaskCaller
from clio_agent.gact.task_history import TaskHistory
from clio_agent.gact.tool_result_presentation import project_presentation
from clio_agent.tools.result_errors import structured_tool_result_error
from clio_agent.tools.task_receipt import accepted_result
from clio_agent.tools.tool_presentation import present_mcp_result

logger = logging.getLogger(__name__)

_STATES = {
    "accepted": "pending",
    "queued": "pending",
    "running": "running",
    "working": "running",
    "active": "running",
    "input_required": "pending",
    "paused": "pending",
    "completed": "succeeded",
    "failed": "failed",
    "cancelled": "cancelled",
    "interrupted": "unknown",
    "task_state_uncertain": "unknown",
    "unknown": "unknown",
}


def own_execution_tasks(
    app: Any, session_id: str, invocation_ids: set[str]
) -> dict[str, tuple[TaskSnapshot, ...]]:
    """Authorize exact invocation joins within one session, excluding descendant work.

    The transcript boot can publish ``task_history`` before the task runtime admits its
    human principal; until then no caller exists to authorize, so there are no joins.
    """
    history = getattr(app.state, "task_history", None)
    principal = getattr(app.state, "human_principal", None)
    if history is None or principal is None or not invocation_ids:
        return {}
    caller = TaskCaller(session_id, principal, human_action=True)
    access = TaskAccess(history, caller)
    result: dict[str, tuple[TaskSnapshot, ...]] = {}
    for handle in history.session_handles(session_id):
        task = history.task_snapshot(handle)
        if (
            task is None
            or task.owner.session_id != session_id
            or task.invocation_id not in invocation_ids
            or task.backend is None
            or task.backend.owner not in {"native", "external_mcp"}
        ):
            continue
        access.require(handle, "read")
        result[task.invocation_id] = (*result.get(task.invocation_id, ()), task)
    return result


def _result_presentation(
    history: TaskHistory, task: TaskSnapshot, tool: Mapping[str, Any]
) -> tuple[dict[str, Any] | None, dict[str, Any]]:
    """Decode the pinned standard MCP reply, retaining public fields only."""
    if task.result is None:
        raise ValueError("The execution ended without a recorded result")
    reply = ClientGetTaskResult.model_validate(history.read_content(task.result))
    if (
        task.backend is None
        or reply.task_id != task.backend.task_id
        or reply.status not in {"completed", "failed", "cancelled"}
    ):
        raise ValueError("The recorded result does not match this terminal execution")
    if reply.result is None:
        if task.status == "cancelled" and reply.status == "cancelled":
            return None, _diagnostic(task.progress.message or "Execution cancelled.")
        if task.status == "failed" and reply.status == "failed":
            return None, _diagnostic(
                task.progress.message or "The execution failed without returning a tool result.",
                failed=True,
            )
        raise ValueError("The completed execution did not return a tool result")
    result = call_tool_result_to_observer(_inlined_call_tool_result(reply.result))
    arguments = tool.get("input")
    args = arguments if isinstance(arguments, Mapping) else {}
    # Task custody retains the server's local tool name. Reconstruct the declared
    # gateway identity rather than guessing result semantics from display labels.
    name = f"{task.backend.server}_{task.tool}" if task.backend.server else task.tool
    presentation = present_mcp_result(name, args, result)
    return result, presentation


def _diagnostic(message: str, *, failed: bool = False) -> dict[str, Any]:
    return {
        "summary": "",
        "blocks": [
            {
                "id": "task-execution-status",
                "type": "text",
                "label": "Execution status",
                "text": message,
                "severity": "error" if failed else "info",
            }
        ],
    }


def project_execution(
    history: TaskHistory, tool: Mapping[str, Any], task: TaskSnapshot
) -> tuple[dict[str, Any], dict[str, Any]]:
    """Project one authorized operation, keeping its submission RPC separately."""
    output = tool.get("output")
    receipt = output.get("structuredContent", output) if isinstance(output, Mapping) else None
    if isinstance(receipt, Mapping):
        try:
            accepted = accepted_result(dict(receipt)).structured_content
        except ValueError:
            accepted = None
        if accepted is not None and accepted.get("handle") != task.handle:
            mismatch = "The submission receipt and execution record name different tasks."
            return {
                **tool,
                "state": "unknown",
                "execution_problem": "submission_task_mismatch",
                "error": mismatch,
                "presentation": _diagnostic(mismatch, failed=True),
            }, _diagnostic(mismatch, failed=True)
    state = _STATES[task.status]
    result: Any = None
    error: str | None = None
    if task.status == "completed" and (task.terminal_at is None or task.result is None):
        state, error = "unknown", "The completed task has no recorded terminal result."
    if task.terminal_at is not None and task.result is not None:
        try:
            result, presentation = _result_presentation(history, task, tool)
        except (ValueError, TypeError, ValidationError) as problem:
            logger.warning(
                "Task outcome unavailable handle=%s reason=task_result_invalid error_type=%s",
                task.handle,
                type(problem).__name__,
            )
            state, error = "unknown", "The recorded execution result could not be decoded."
            presentation = _diagnostic(error, failed=True)
        else:
            error = structured_tool_result_error(result)
            if error and state == "succeeded":
                state = "failed"
            if state == "failed" and not error:
                error = task.progress.message or "The execution owner reported failure."
                presentation["blocks"].extend(_diagnostic(error, failed=True)["blocks"])
            presentation["status"] = state
    else:
        message = (
            error
            or task.progress.message
            or {
                "input_required": "Waiting for input.",
                "paused": "Execution is paused.",
                "accepted": "Work accepted; no completed result yet.",
                "queued": "Work is queued.",
            }.get(task.status, "Execution is " + task.status.replace("_", " ") + ".")
        )
        if task.cancellation_requested_at and task.terminal_at is None:
            message = "Cancellation requested; settlement is not yet confirmed. " + message
        presentation = _diagnostic(message, failed=state in {"failed", "unknown"})
    original_presentation = tool.get("presentation") or {}
    if original_presentation.get("action") and not presentation.get("action"):
        presentation["action"] = original_presentation["action"]
    # A receipt can have its own blocks. These addresses belong to the execution.
    presentation = {
        **presentation,
        "blocks": [
            {**block, "id": "task-result-" + block["id"]}
            for block in presentation.get("blocks", [])
        ],
    }
    if presentation.get("subject"):
        presentation["subject"] = "task-result-" + presentation["subject"]
    projected_presentation = (
        project_presentation(presentation, task.owner.session_id, str(tool["id"]))
        if task.result is not None
        else presentation
    )
    for block in (projected_presentation or {}).get("blocks", []):
        if block.get("content_ref") and task.result is not None:
            block["content_ref"]["result_sha256"] = task.result.sha256
    entity = {
        **tool,
        "state": state,
        "output": _bounded_tool_call_result(result),
        "submission": {
            key: tool[key]
            for key in ("state", "output", "error", "completed_at", "duration_ms")
            if key in tool
        },
        "execution_task": {
            "handle": task.handle,
            "status": task.status,
            "activity": task.activity,
            "updated_at": task.updated_at,
            "accepted_at": task.created_at,
            "terminal_at": task.terminal_at,
            "cancellation_requested_at": task.cancellation_requested_at,
            "result_sha256": task.result.sha256 if task.result is not None else None,
        },
        "presentation": projected_presentation,
    }
    entity.pop("error", None)
    entity.pop("completed_at", None)
    entity.pop("duration_ms", None)
    if error:
        entity["error"] = error
    if task.terminal_at is not None:
        entity["completed_at"] = task.terminal_at
        if started := tool.get("started_at"):
            try:
                entity["duration_ms"] = max(
                    0.0,
                    (
                        datetime.fromisoformat(task.terminal_at)
                        - datetime.fromisoformat(str(started))
                    ).total_seconds()
                    * 1000,
                )
            except (ValueError, TypeError):
                logger.warning(
                    "Execution timing unavailable handle=%s reason=invalid_timestamp", task.handle
                )
    return entity, presentation


def join_task_executions(
    app: Any, session_id: str, tools: Sequence[Mapping[str, Any]]
) -> list[dict[str, Any]]:
    """Rebuild operation views from the same owned task records on every read."""
    matches = own_execution_tasks(app, session_id, {str(tool["id"]) for tool in tools})
    result = []
    for tool in tools:
        tasks = matches.get(str(tool["id"]), ())
        if len(tasks) == 1:
            entity, _presentation = project_execution(app.state.task_history, tool, tasks[0])
            result.append(entity)
        elif tasks:
            result.append(
                {
                    **tool,
                    "state": "unknown",
                    "error": "Multiple execution tasks claim this invocation; inspect the task records.",
                    "execution_problem": "ambiguous_execution_custody",
                }
            )
        else:
            result.append(dict(tool))
    return result


def execution_presentation(
    app: Any,
    session_id: str,
    call_id: str,
    name: str,
    arguments: Mapping[str, Any] | None,
    *,
    result_sha256: str | None = None,
) -> dict[str, Any] | None:
    """Resolve task-owned presentation pages through the same authorized join."""
    matches = own_execution_tasks(app, session_id, {call_id}).get(call_id, ())
    if len(matches) != 1:
        return None
    if result_sha256 is not None and (
        matches[0].result is None or matches[0].result.sha256 != result_sha256
    ):
        raise ValueError("The task result changed; reload the result before continuing")
    _entity, presentation = project_execution(
        app.state.task_history,
        {"id": call_id, "name": name, "input": arguments, "state": "unknown"},
        matches[0],
    )
    return presentation


def completion_execution(
    app: Any, session_id: str, payload: Mapping[str, Any]
) -> dict[str, Any] | None:
    """Join a live RPC completion to its current execution using recorded call context."""
    call_id = str(payload.get("call_id") or "")
    tasks = own_execution_tasks(app, session_id, {call_id}).get(call_id, ())
    if not tasks:
        return None
    rows = getattr(app.state, "tool_call_ledger", {}).get(session_id, ())
    recorded: Mapping[str, Any] = next(
        (row for row in reversed(rows) if row.get("call_id") == call_id), {}
    )
    parts = getattr(app.state, "live_assistant_parts", {}).get(session_id, ())
    call = next(
        (part for part in parts if part.call_id == call_id and part.type == "tool_call"), None
    )
    if call is None:
        call = next(
            (
                part
                for message in app.state.messages.get(session_id, ())
                for part in message.parts
                if part.call_id == call_id and part.type == "tool_call"
            ),
            None,
        )
    metadata = call.metadata if call is not None else {}
    entity = {
        "id": call_id,
        "session_id": session_id,
        "name": str(payload.get("tool") or "Tool"),
        "state": "succeeded" if payload.get("ok") else "failed",
        "input": recorded.get("args", call.input if call is not None else {}),
        "output": payload.get("result"),
        **({"title": payload["tool_title"]} if payload.get("tool_title") else {}),
        **(
            {
                key: payload[key]
                for key in ("completed_at", "duration_ms", "error", "presentation")
                if key in payload
            }
        ),
        **({"started_at": metadata["started_at"]} if metadata.get("started_at") else {}),
    }
    if len(tasks) != 1:
        return {
            **entity,
            "state": "unknown",
            "execution_problem": "ambiguous_execution_custody",
            "error": "Multiple execution tasks claim this invocation.",
        }
    return project_execution(app.state.task_history, entity, tasks[0])[0]
