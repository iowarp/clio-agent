"""Reconnect an accepted task to its original durable input question and deadline."""

from __future__ import annotations

import logging
from collections.abc import Mapping
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from typing import Any

from clio_agent.gact.types import UserQuestion
from clio_agent.tools.mcp_handlers import MCPInvocationContext
from clio_agent.tools.mcp_task_records import TaskKey, TaskRecord, iter_task_records

logger = logging.getLogger(__name__)


class TaskInputIdentityError(ValueError):
    """An input cannot be safely attributed to exactly one accepted task."""


def _owned_record(app: Any, invocation: MCPInvocationContext) -> TaskRecord:
    store = getattr(getattr(app.state, "sessions", None), "task_store", None)
    if store is None:
        raise TaskInputIdentityError("Task input has no app-owned durable store")
    if invocation.task_key is not None:
        row = store.get(invocation.task_key)
        candidates = [row] if row is not None else []
    else:
        candidates = list(iter_task_records(store))
    candidates = [
        row
        for row in candidates
        if row.session_id == invocation.session_id
        and row.key.server_id == invocation.namespace
        and row.task_id == invocation.task_id
        and row.invocation_id == invocation.invocation_id
        and row.tool == invocation.tool_name
    ]
    if len(candidates) != 1:
        raise TaskInputIdentityError("Task input has absent or ambiguous accepted ownership")
    return candidates[0]


def _matches(question: UserQuestion, invocation: MCPInvocationContext) -> bool:
    metadata = question.metadata.get("elicitation")
    return isinstance(metadata, Mapping) and all(
        (
            (question.owner_session_id or question.session_id) == invocation.session_id,
            question.source == "mcp_elicitation",
            metadata.get("namespace") == invocation.namespace,
            metadata.get("tool_name") == invocation.tool_name,
            metadata.get("invocation_id") == invocation.invocation_id,
            metadata.get("task_id") == invocation.task_id,
            metadata.get("input_key") == invocation.input_key,
        )
    )


def recover_input_question(
    app: Any,
    invocation: MCPInvocationContext,
    question: UserQuestion,
    timeout: float,
) -> tuple[UserQuestion, bool, float]:
    """Reuse the exact task/input row; never consume another backend's answer.

    Legacy rows are eligible only when their original invocation resolves uniquely
    in this app's store. Full keys distinguish colliding backend session identities.
    Recovery retains the first question's human deadline rather than resetting it.
    """
    if not invocation.task_id or not invocation.input_key:
        return question, True, timeout
    row = _owned_record(app, invocation)
    matches = []
    for candidate in stored_input_questions(app, invocation):
        if not _matches(candidate, invocation):
            continue
        raw_key = candidate.metadata["elicitation"].get("task_key")
        if isinstance(raw_key, Mapping):
            if TaskKey.from_wire(raw_key) != row.key:
                continue
        else:
            # A new full-key caller must not bless an ambiguous legacy question.
            legacy = MCPInvocationContext(
                invocation.invocation_id,
                invocation.session_id,
                invocation.namespace,
                invocation.tool_name,
                task_id=invocation.task_id,
            )
            if _owned_record(app, legacy).key != row.key:
                raise TaskInputIdentityError("Legacy input does not belong to this task")
        matches.append(candidate)
    if len(matches) > 1:
        raise TaskInputIdentityError("Multiple durable questions represent the same task input")
    publish = not matches
    question = matches[0] if matches else question
    if not publish:
        return question, False, _remaining(question, timeout)
    metadata = dict(question.metadata)
    metadata["elicitation"] = {**metadata["elicitation"], "task_key": row.key.to_wire()}
    deadline = datetime.now(timezone.utc) + timedelta(seconds=timeout)
    question = question.model_copy(
        update={"metadata": metadata, "expires_at": deadline.isoformat()}
    )
    return question, True, timeout


def _remaining(question: UserQuestion, timeout: float) -> float:
    if question.status != "pending":
        return 0.0
    try:
        deadline = (
            datetime.fromisoformat(question.expires_at)
            if question.expires_at
            else (datetime.fromisoformat(question.created_at) + timedelta(seconds=timeout))
        )
        return max(0.0, (deadline - datetime.now(timezone.utc)).total_seconds())
    except (ValueError, TypeError) as exc:
        raise TaskInputIdentityError("Stored task input deadline is invalid") from exc


def preserve_input_on_disconnect(app: Any, invocation: MCPInvocationContext) -> bool:
    """A recoverable driver losing its waiter does not cancel the human's input."""
    if not invocation.task_id or not invocation.input_key:
        return False
    try:
        row = _owned_record(app, invocation)
    except TaskInputIdentityError as exc:
        logger.warning("Task input cannot survive disconnect reason=owner_absent error=%s", exc)
        return False
    return (
        row.backend.get("transport") in {"http", "streamable-http"}
        and not row.cancel_requested
        and row.display_status not in {"completed", "failed", "cancelled", "interrupted"}
    )


def stored_input_questions(app: Any, invocation: MCPInvocationContext) -> list[UserQuestion]:
    """Read the owner's durable rows as well as the bounded process projection."""
    rows = dict(app.state.user_questions)
    session = app.state.sessions.get(invocation.session_id)
    metadata = getattr(session, "metadata", {}) or {}
    durable = metadata.get("user_questions", {})
    if isinstance(durable, Mapping):
        for question_id, raw in durable.items():
            if question_id not in rows and isinstance(raw, Mapping):
                rows[question_id] = UserQuestion.model_validate(raw)
    return list(rows.values())


def input_needs_delivery(app: Any, raw: Mapping[str, Any]) -> bool:
    """Pin answered task inputs until their owner stores the backend update receipt."""
    metadata = raw.get("metadata", {})
    elicitation = metadata.get("elicitation", {}) if isinstance(metadata, Mapping) else {}
    if raw.get("source") != "mcp_elicitation" or not isinstance(elicitation, Mapping):
        return False
    if not elicitation.get("task_id") or not elicitation.get("input_key"):
        return False
    invocation = MCPInvocationContext(
        str(elicitation.get("invocation_id") or ""),
        raw.get("owner_session_id") or raw.get("session_id"),
        elicitation.get("namespace"),
        elicitation.get("tool_name"),
        task_id=elicitation["task_id"],
        input_key=elicitation["input_key"],
        task_key=(
            TaskKey.from_wire(elicitation["task_key"])
            if isinstance(elicitation.get("task_key"), Mapping)
            else None
        ),
    )
    try:
        row = _owned_record(app, invocation)
    except TaskInputIdentityError:
        # Ambiguous old ownership must be diagnosed during recovery, not evicted
        # from the only durable answer surface before it can be inspected.
        return True
    return row.display_status not in {
        "completed",
        "failed",
        "cancelled",
        "interrupted",
    } and not any(
        answer.key == invocation.input_key and answer.delivered for answer in row.input_answers
    )


def cancel_task_inputs(app: Any, key: TaskKey) -> int:
    """Close and wake only pending questions belonging to an explicitly cancelled task.

    The first-wins question transition preserves a committed human answer. Waking
    its input waiter resumes backend observation; it never settles the task itself.
    """
    from clio_agent.gact.elicitation_bridge import _terminalize_question, resolve_elicitation
    from clio_agent.gact.user_question_ledger import record_user_question

    store = getattr(getattr(app.state, "sessions", None), "task_store", None)
    row = store.get(key) if store is not None else None
    if row is None or not row.cancel_requested:
        return 0
    invocation = MCPInvocationContext(
        row.invocation_id,
        key.session_id,
        key.server_id,
        row.tool,
        task_id=key.task_id,
        task_key=key,
    )
    cancelled = 0
    for question in stored_input_questions(app, invocation):
        metadata = question.metadata.get("elicitation", {})
        if not isinstance(metadata, Mapping) or question.status != "pending":
            continue
        candidate_invocation = replace(invocation, input_key=metadata.get("input_key"))
        if not _matches(question, candidate_invocation):
            continue
        raw_key = metadata.get("task_key")
        if isinstance(raw_key, Mapping):
            if TaskKey.from_wire(raw_key) != key:
                continue
        else:
            legacy = MCPInvocationContext(
                row.invocation_id, key.session_id, key.server_id, row.tool, task_id=key.task_id
            )
            try:
                if _owned_record(app, legacy).key != key:
                    continue
            except TaskInputIdentityError as exc:
                logger.warning("Cancelled task input has ambiguous legacy ownership error=%s", exc)
                continue
        if question.id not in app.state.user_questions:
            record_user_question(app, question)
        if _terminalize_question(app, question.id, "cancelled", "user_question.cancelled"):
            resolve_elicitation(app, app.state.user_questions[question.id])
            cancelled += 1
    return cancelled


def cancel_published_task_input(app: Any, invocation: MCPInvocationContext) -> bool:
    """Close the publication race when cancellation preceded the question's creation."""
    if not invocation.task_id or not invocation.input_key:
        return False
    row = _owned_record(app, invocation)
    if not row.cancel_requested:
        return False
    cancel_task_inputs(app, row.key)
    return True
