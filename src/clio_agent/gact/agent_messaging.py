"""Placement-driven message-an-agent queue, steer, and wake semantics (#1128)."""

from __future__ import annotations

import json
import uuid
from collections.abc import Mapping
from dataclasses import asdict, dataclass
from typing import Any

from fastapi import HTTPException

from clio_agent.errors import ToolError
from clio_agent.gact import context as _ctx
from clio_agent.gact.agent_tasks import (
    STATUS_COMPLETED,
    AgentTask,
)
from clio_agent.gact.agents.invoker import InvokerError, TaskHandle, TaskSpec
from clio_agent.gact.agents.spawn_placement import invoker_for_task, run_handle_fields
from clio_agent.gact.events import Event
from clio_agent.gact.runtime.globals import _active_semantic_turn_id
from clio_agent.gact.spawn_context import bind_task_spec_to_parent
from clio_agent.gact.tool_observer import _append_live_assistant_part
from clio_agent.gact.turn_spawn import SpawnError
from clio_agent.gact.types import Part


class MessageAgentError(Exception):
    """A typed message refusal shared by the route and model-facing tool."""

    def __init__(
        self,
        message: str,
        *,
        reason: str,
        details: Mapping[str, Any] | None = None,
        status_code: int = 409,
        recoverable: bool = True,
    ) -> None:
        super().__init__(message)
        self.reason = reason
        self.details = dict(details or {})
        self.status_code = status_code
        self.recoverable = recoverable


@dataclass(frozen=True)
class MessageAgentResult:
    """Uniform result for a queued injection or finished-child wake."""

    accepted: bool
    action: str
    transport: str
    task_id: str
    child_session_id: str
    handle_id: str
    run_label: str
    live_state: str
    host: str
    placement: str
    supersedes_task_id: str = ""

    def to_wire(self) -> dict[str, Any]:
        """Return the JSON-safe route/tool result."""

        return asdict(self)


def _require_message(text: str) -> str:
    message = str(text or "")
    if not message.strip():
        raise MessageAgentError(
            "agent message must be non-empty",
            reason="invalid_request",
            details={"field": "text"},
            status_code=422,
        )
    return message


def _http_refusal(exc: HTTPException) -> MessageAgentError:
    detail: Mapping[str, Any] = exc.detail if isinstance(exc.detail, Mapping) else {}
    raw_error = detail.get("error")
    envelope: Mapping[str, Any] = raw_error if isinstance(raw_error, Mapping) else detail
    error_details = dict(envelope.get("details") or {})
    reason = str(envelope.get("error") or "message_refused")
    message = str(envelope.get("message") or exc.detail)
    return MessageAgentError(
        message,
        reason=reason,
        details=error_details,
        status_code=exc.status_code,
        recoverable=bool(envelope.get("recoverable", True)),
    )


def _typed_refusal(exc: Exception) -> MessageAgentError:
    reason = str(getattr(exc, "reason", "") or "")
    details = dict(getattr(exc, "details", {}) or {})
    reason = reason or str(details.get("reason") or "message_transport_error")
    return MessageAgentError(str(exc), reason=reason, details=details)


def _message_part(
    task: AgentTask,
    *,
    text: str,
    transport: str,
    parent_agent_id: str,
) -> Part:
    child_id = task.agent_ref.get("expert_id", "")
    fields = run_handle_fields(task, child_id)
    return Part(
        id=f"live_agent_message_{uuid.uuid4().hex[:12]}",
        type="agent_message",
        agent_id=parent_agent_id,
        parent_agent=parent_agent_id,
        child_agent=child_id,
        stage="message.queued",
        handle_id=fields["handle_id"],
        run_label=fields["run_label"],
        live_state=fields["live_state"],
        host=fields["host"],
        placement=fields["placement"],
        message_action="queue",
        status="accepted",
        text=text,
        metadata={"message_action": "queue", "transport": transport},
    )


def _supersede_part(
    old: AgentTask,
    new: TaskHandle,
    *,
    parent_agent_id: str,
) -> Part:
    child_id = old.agent_ref.get("expert_id", "")
    fields = run_handle_fields(new, child_id)
    return Part(
        id=f"live_agent_supersede_{uuid.uuid4().hex[:12]}",
        type="expert_handoff",
        agent_id=parent_agent_id,
        parent_agent=parent_agent_id,
        child_agent=child_id,
        stage="delegate.superseded",
        handle_id=fields["handle_id"],
        run_label=fields["run_label"],
        live_state=fields["live_state"],
        host=fields["host"],
        placement=fields["placement"],
        message_action="wake",
        supersedes_handle_id=old.task_id,
        superseded_by_handle_id=new.task_id,
        status="superseded",
        text=f"{parent_agent_id} -> {child_id}",
        metadata={"message_action": "wake", "transport": "spawn"},
    )


def _wake_finished(
    app: Any,
    task: AgentTask,
    message: str,
    *,
    parent_agent_id: str,
) -> MessageAgentResult:
    if task.status != STATUS_COMPLETED:
        raise MessageAgentError(
            f"terminal task {task.task_id!r} cannot be woken from status {task.status!r}",
            reason="task_unwakeable",
            details={"task_id": task.task_id, "status": task.status},
            recoverable=False,
        )
    if app.state.sessions.get(task.parent_session_id) is None:
        raise MessageAgentError(
            f"parent session for task {task.task_id!r} is gone",
            reason="parent_session_gone",
            details={"task_id": task.task_id, "parent_session_id": task.parent_session_id},
            recoverable=False,
        )
    if app.state.sessions.get(task.child_session_id) is None:
        raise MessageAgentError(
            f"child session for task {task.task_id!r} is gone",
            reason="child_session_gone",
            details={"task_id": task.task_id, "child_session_id": task.child_session_id},
            recoverable=False,
        )
    try:
        binding = invoker_for_task(app, task)
        spec = bind_task_spec_to_parent(
            app,
            TaskSpec(
                child_expert_id=task.agent_ref.get("expert_id", ""),
                task_text=message,
                parent_session_id=task.parent_session_id,
                requesting_expert_id=(
                    task.agent_ref.get("requesting_expert_id") or parent_agent_id
                ),
                parent_turn_id=_active_semantic_turn_id(),
                depth=task.depth,
                mode="async",
                placement=binding.placement,
            ),
        )
        spawned = binding.invoker.invoke(spec)
    except (InvokerError, SpawnError, ToolError) as exc:
        raise _typed_refusal(exc) from exc

    payload = {
        "task_id": task.task_id,
        "supersedes_task_id": task.task_id,
        "superseded_by_task_id": spawned.task_id,
        "parent_session_id": task.parent_session_id,
        "child_session_id": task.child_session_id,
        "woken_child_session_id": spawned.child_session_id,
        "agent_id": task.agent_ref.get("expert_id", ""),
        "placement": spawned.placement,
        "status": "superseded",
    }
    app.state.bus.publish(
        Event(
            type="agent.task.superseded",
            session_id=task.parent_session_id,
            payload=payload,
        )
    )
    _append_live_assistant_part(
        app,
        task.parent_session_id,
        _supersede_part(task, spawned, parent_agent_id=parent_agent_id),
    )
    fields = run_handle_fields(spawned, task.agent_ref.get("expert_id", ""))
    return MessageAgentResult(
        accepted=True,
        action="wake",
        transport="spawn",
        task_id=spawned.task_id,
        child_session_id=spawned.child_session_id,
        supersedes_task_id=task.task_id,
        **fields,
    )


def message_agent_task(
    app: Any,
    task_id: str,
    text: str,
    metadata: Mapping[str, Any] | None = None,
    *,
    parent_agent_id: str = "",
) -> MessageAgentResult:
    """Message a task without naming its transport.

    Running local tasks queue into the existing step-boundary steer inbox; running
    relay tasks answer the parked tasks/update round. A completed task creates a
    placement-matched successor run and types the earlier return as superseded.
    """

    message = _require_message(text)
    task = app.state.agent_task_registry.get(task_id)
    if task is None:
        raise MessageAgentError(
            f"unknown agent task {task_id!r}",
            reason="unknown_task",
            details={"task_id": task_id},
            status_code=404,
            recoverable=False,
        )
    actor = parent_agent_id or task.agent_ref.get("requesting_expert_id") or "main"
    if task.is_terminal:
        if metadata:
            raise MessageAgentError(
                "wake messages do not carry mid-turn steer metadata",
                reason="message_metadata_unsupported",
                details={"task_id": task.task_id, "action": "wake"},
            )
        return _wake_finished(app, task, message, parent_agent_id=actor)

    try:
        binding = invoker_for_task(app, task)
        transport = "tasks/update" if binding.placement.startswith("relay:") else "step_boundary"
        binding.invoker.message(TaskHandle.from_task(task), message, dict(metadata or {}))
    except HTTPException as exc:
        raise _http_refusal(exc) from exc
    except (InvokerError, SpawnError, ToolError) as exc:
        raise _typed_refusal(exc) from exc
    _append_live_assistant_part(
        app,
        task.parent_session_id,
        _message_part(task, text=message, transport=transport, parent_agent_id=actor),
    )
    fields = run_handle_fields(task, task.agent_ref.get("expert_id", ""))
    return MessageAgentResult(
        accepted=True,
        action="queue",
        transport=transport,
        task_id=task.task_id,
        child_session_id=task.child_session_id,
        **fields,
    )


def build_message_agent_tool(agent_def: Any) -> Any:
    """Build the one model-facing message tool bound to the requesting expert."""

    import dspy  # noqa: PLC0415

    def message_agent(task_id: str, message: str) -> str:
        """Send a message to a spawned child by task id.

        Transport is selected from the task's retained placement. A running child
        receives the message at its next safe boundary; a completed child starts a
        placement-matched successor run and supersedes its earlier return.
        """

        app = _ctx.active_app()
        session_id = _ctx.active_session_id()
        if app is None or not session_id:
            raise RuntimeError("message_agent requires an active CLIO app/session context")
        try:
            result = message_agent_task(
                app,
                task_id,
                message,
                parent_agent_id=str(getattr(agent_def, "id", "") or "main"),
            )
            return json.dumps(result.to_wire(), sort_keys=True)
        except MessageAgentError as exc:
            return json.dumps(
                {
                    "error": exc.reason,
                    "message": str(exc),
                    "details": exc.details,
                },
                sort_keys=True,
            )

    return dspy.Tool(
        func=message_agent,
        name="message_agent",
        desc=message_agent.__doc__,
        args={
            "task_id": {"type": "string", "description": "Task id returned by spawn."},
            "message": {"type": "string", "description": "Message for the child agent."},
        },
    )
