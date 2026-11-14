"""Real Core assignment records for retained non-execution API regressions."""

from __future__ import annotations

import asyncio
import threading
from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any, Literal
from uuid import uuid4

import pytest

from clio_agent.arc.task_types import (
    AssignmentDetails,
    BackendReference,
    MailItem,
    TaskOwner,
    TaskSnapshot,
)
from clio_agent.gact import context
from clio_agent.gact.native_work import now

_ADMIT_WAIT_S = 120.0


def hold_model_segment(app: Any, session_id: str, monkeypatch: pytest.MonkeyPatch) -> None:
    """Occupy a real runner slot from Core mail; replace only model execution."""
    from tests.test_gact.test_post_messages import FakeClioAgent

    entered = threading.Event()

    async def hold(_segment: Any) -> None:
        entered.set()
        await asyncio.Event().wait()

    monkeypatch.setattr(app.state, "agent", FakeClioAgent())
    monkeypatch.setattr(app.state.conversations, "_run", hold)
    history = app.state.task_history
    history.record_mail(
        MailItem(
            id="fixture-busy:" + uuid4().hex,
            recipient_session=session_id,
            sender_human=app.state.human_principal,
            kind="human",
            content=history.write_content(session_id, {"text": "Hold this model segment."}),
            created_at=now(),
        )
    )
    # Admission does real pre-model work (tool catalog, context) before `_run`; a loaded
    # CI runner has taken over 5 s. Bound only real hangs, under the 180 s per-test guard.
    assert entered.wait(_ADMIT_WAIT_S), (
        "The real runner did not admit the initial input",
        {
            "execution_gate": app.state.conversations.execution_gate(session_id),
            "restoring": app.state.conversations._restoring,
            "requires_recovery": history.requires_recovery,
            "busy": app.state.turn_runner.busy(session_id),
            "session_metadata": app.state.sessions.get(session_id).metadata,
            "pending_mail": [
                item.model_dump(mode="json") for item in history.pending_mail(session_id)
            ],
        },
    )
    assert app.state.turn_runner.busy(session_id)


@contextmanager
def active_turn_fixture(app: Any, session_id: str) -> Iterator[None]:
    """Bind an existing recorded assignment for a test call; start no work."""
    app_token = context.set_app(app)
    session_token = context.set_session_id(session_id)
    try:
        history = getattr(app.state, "task_history", None)
        task = history.latest_assignment(session_id) if history is not None else None
        if task is not None and history is not None:
            details = (
                AssignmentDetails.model_validate(history.read_content(task.details))
                if task.details is not None
                else AssignmentDetails()
            )
            context.set_turn_identity(
                app=app,
                session_id=session_id,
                turn_id="fixture-turn",
                trace_id="fixture-trace",
                assignment_handle=task.handle,
                context_epoch=details.context_epoch,
            )
        yield
    finally:
        context.reset(session_token)
        context.reset(app_token)


def record_assignment_fixture(
    app: Any,
    session_id: str,
    *,
    handle: str | None = None,
    parent_sid: str | None = None,
    parent_turn_id: str | None = None,
    status: Literal["active", "completed"] = "active",
    description: str | None = "Retained API fixture assignment",
    label: str | None = None,
    resource_id: str | None = None,
    result: dict[str, Any] | None = None,
) -> TaskSnapshot:
    """Confirm passive test data through the production writer; create no model worker."""
    history = app.state.task_history
    with history.serialized():
        parent = history.latest_assignment(parent_sid) if parent_sid is not None else None
        if parent_sid is not None and parent is None:
            parent = record_assignment_fixture(app, parent_sid)
        session = app.state.sessions.get(session_id)
        if session is None:
            raise ValueError("Assignment fixture requires a real retained session")
        identity, stamp = handle or "task_" + uuid4().hex, now()
        task = TaskSnapshot(
            handle=identity,
            kind="Subagent" if parent is not None else "Session",
            owner=TaskOwner(
                human_id=app.state.human_principal,
                session_id=session_id,
                assignment_id=identity,
                parent_handle=parent.handle if parent is not None else None,
            ),
            tool="conversation",
            label=label or session.title,
            description=description,
            description_missing=description is None,
            resource_id=resource_id,
            invocation_id="fixture:" + identity,
            created_at=stamp,
            updated_at=stamp,
            raw_status=status,
            status=status,
            activity="idle" if status == "active" else "",
            terminal_at=stamp if status == "completed" else None,
            settlement="confirmed" if status == "completed" else "unknown",
            backend=BackendReference(
                owner="conversation", server="clio", session=session_id, task_id=identity
            ),
            details=history.write_content(
                session_id, AssignmentDetails(parent_turn_id=parent_turn_id).model_dump(mode="json")
            ),
            result=history.write_content(
                session_id, result if result is not None else {"answer": "Fixture completed"}
            )
            if status == "completed"
            else None,
        )
        history.record_snapshot(task, observation_id="fixture:" + identity)
        confirmed = history.task_snapshot(identity)
        assert confirmed is not None
        return confirmed


def record_delegation_fixture(
    app: Any,
    parent: str,
    *,
    handle: str,
    agent: str = "worker",
    label: str = "Analyze the sample",
    description: str | None = "Analyze the sample",
    parent_turn_id: str | None = "fixture-turn",
    completed: bool = False,
    result: dict[str, Any] | None = None,
    resource_id: str | None = None,
) -> TaskSnapshot:
    """Write retained child attribution through Core without executing a model."""
    child = app.state.sessions.create(
        workspace_id=app.state.sessions.get(parent).workspace_id,
        title=label,
        parent_session_id=parent,
        agent={"id": agent, "mode": "subagent"},
    )
    task = record_assignment_fixture(
        app,
        child.id,
        handle=handle,
        parent_sid=parent,
        parent_turn_id=parent_turn_id,
        status="completed" if completed else "active",
        description=description,
        label=label,
        resource_id=resource_id,
        result=result,
    )
    return task


def record_question_fixture(app: Any, row: Any) -> Any:
    """Confirm retained Question data through its production owner; return only a view."""
    from clio_agent.arc.task_types import QuestionDefinition, QuestionOption, QuestionReply
    from clio_agent.gact.question_surface import question_row

    assignment = app.state.task_history.latest_assignment(
        row.session_id
    ) or record_assignment_fixture(app, row.session_id)
    details = (
        AssignmentDetails.model_validate(app.state.task_history.read_content(assignment.details))
        if assignment.details
        else AssignmentDetails()
    )
    metadata = dict(row.metadata or {})
    if row.source == "plan_exit":
        metadata["plan_approval"] = {
            "assignment": assignment.handle,
            "context_epoch": details.context_epoch,
            "request": {
                "summary": row.prompt,
                "plan_file": metadata.get("plan_file", ""),
                "recommended_mode": metadata.get("recommended_mode", "auto"),
                "invocation_id": "fixture-plan:" + row.id,
                "surfaced": True,
            },
        }
    definition = QuestionDefinition(
        prompt=row.prompt,
        kind=row.kind,
        source=row.source or "ask_user",
        options=tuple(
            QuestionOption(
                label=option.label, value=option.value, description=option.description or ""
            )
            for option in (row.options or ())
        ),
        allow_freeform=row.allow_freeform,
        expires_at=row.expires_at or None,
        metadata=metadata,
    )
    task = app.state.questions.create(
        assignment.owner.model_copy(update={"parent_handle": assignment.handle}),
        definition,
        invocation_id=str(metadata.get("invocation_id") or row.id),
        handle=row.id,
    )
    if row.status == "answered":
        task = app.state.questions.answer(
            task.handle,
            QuestionReply(
                answer=row.answer or "",
                selected_options=tuple(row.selected_options or ()),
                metadata=dict(row.answer_metadata or {}),
                responder_human=app.state.human_principal,
            ),
        )
    elif row.status == "cancelled":
        task = app.state.questions.cancel(task.handle)
    elif row.status != "pending":
        raise ValueError("This fixture needs an explicit owner transition for " + row.status)
    return question_row(app, task.handle)


def record_composer_fixture(
    app: Any,
    sid: str,
    identity: str,
    *,
    text: str = "",
    parts: list[Any] | None = None,
    client_message_id: str = "",
) -> MailItem:
    """Confirm future-message data through Core without a removed JSON intent store."""
    from clio_agent.gact.types import PostMessageRequest

    request = PostMessageRequest(text=text, parts=parts or [], client_message_id=client_message_id)
    item = MailItem(
        id=identity,
        recipient_session=sid,
        kind="composer",
        sender_human=app.state.human_principal,
        delivery="queued",
        queued_order=0,
        content=app.state.task_history.write_content(
            sid, {"request": request.model_dump(mode="json"), "updated_at": now()}
        ),
        created_at=now(),
    )
    return app.state.task_history.record_mail(item)
