"""Task inputs retain their original durable question across driver recovery."""

from __future__ import annotations

import asyncio
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Literal

import pytest
from fastapi import FastAPI

from clio_agent.gact import elicitation_bridge as bridge
from clio_agent.gact.elicitation_bridge import handle_elicitation
from clio_agent.gact.elicitation_correlation import invocation_with_request_correlation
from clio_agent.gact.events import EventBus
from clio_agent.gact.sessions import SessionStore
from clio_agent.gact.types import UserQuestion
from clio_agent.gact.user_question_ledger import record_user_question, restore_user_questions
from clio_agent.tools.mcp_handlers import MCPInvocationContext
from clio_agent.tools.mcp_task_records import (
    TaskInputAnswer,
    TaskKey,
    TaskRecord,
    set_task_record_store,
)


@pytest.fixture
def owner(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Any:
    """Use real durable session/question/task stores without a provider or network."""
    monkeypatch.setenv("CLIO_MCP_ELICITATION_AGENT_AUDIENCE_DEFAULT_UNHINTED", "0")
    app = FastAPI()
    app.state.sessions = SessionStore(path=tmp_path / "sessions.json")
    app.state.user_questions = {}
    app.state.bus = EventBus()
    session = app.state.sessions.create(workspace_id="ws", title="input recovery")
    key = TaskKey("server-a", session.id, "backend-task", "backend-session-a")
    assert app.state.sessions.task_store is not None
    app.state.sessions.task_store.put(
        TaskRecord(key, tool="guarded", invocation_id="inv-original", status="input_required")
    )
    yield app, key
    set_task_record_store(None)


def question_for(
    key: TaskKey,
    *,
    status: Literal["pending", "answered", "cancelled", "expired"] = "answered",
) -> UserQuestion:
    """Create the exact persisted input shape seen in the failing live baseline."""
    return UserQuestion(
        id="original-question",
        session_id=key.session_id or "",
        owner_session_id=key.session_id or "",
        prompt="Choose a value",
        created_at=datetime.now(timezone.utc).isoformat(),
        updated_at=datetime.now(timezone.utc).isoformat(),
        source="mcp_elicitation",
        status=status,
        answer_metadata={"value": "chosen-live-value"} if status == "answered" else {},
        metadata={
            "elicitation": {
                "mode": "form",
                "namespace": key.server_id,
                "tool_name": "guarded",
                "invocation_id": "inv-original",
                "task_id": key.task_id,
                "input_key": "input-1",
                "fields": [{"name": "value", "type": "string"}],
                "task_key": key.to_wire(),
            }
        },
    )


def invocation_for(key: TaskKey) -> MCPInvocationContext:
    """Use the compatibility context shape, resolved only from this app's exact owner."""
    return MCPInvocationContext(
        "inv-original",
        key.session_id,
        key.server_id,
        "guarded",
        task_id=key.task_id,
        input_key="input-1",
    )


async def test_recovered_task_uses_persisted_answer_without_another_question(owner: Any) -> None:
    """A new driver reuses an answered durable question before asking the human."""
    app, key = owner
    record_user_question(app, question_for(key))
    app.state.user_questions.clear()
    assert restore_user_questions(app) == 1
    result = await handle_elicitation(
        app,
        invocation_for(key),
        "Choose a value",
        SimpleNamespace(
            mode="form",
            requested_schema={"type": "object", "properties": {"value": {"type": "string"}}},
        ),
        timeout=0.01,
    )
    assert result.action == "accept"
    assert result.content == {"value": "chosen-live-value"}
    assert list(app.state.user_questions) == ["original-question"]


async def call_input(app: Any, invocation: MCPInvocationContext, timeout: float = 0.01) -> Any:
    """Exercise the production input bridge with one actual form schema."""
    return await handle_elicitation(
        app,
        invocation,
        "Choose a value",
        SimpleNamespace(
            mode="form",
            requested_schema={"type": "object", "properties": {"value": {"type": "string"}}},
        ),
        timeout=timeout,
    )


async def test_legacy_answer_reused_only_for_unambiguous_owner(owner: Any) -> None:
    """The pre-full-key live record remains readable without asking twice."""
    app, key = owner
    question = question_for(key)
    del question.metadata["elicitation"]["task_key"]
    record_user_question(app, question)
    result = await call_input(app, replace(invocation_for(key), task_key=key))
    assert result.action == "accept"
    assert result.content == {"value": "chosen-live-value"}
    assert list(app.state.user_questions) == [question.id]


@pytest.mark.parametrize("field", ["server_id", "backend_session_id"])
async def test_full_identity_never_uses_colliding_backend_answer(owner: Any, field: str) -> None:
    """Equal raw task/input/invocation ids cannot cross server or backend sessions."""
    app, key = owner
    other = replace(key, **{field: "other-backend"})
    original = question_for(key)
    colliding = question_for(other).model_copy(
        update={"id": "other-question", "answer_metadata": {"value": "other-answer"}}
    )
    record_user_question(app, colliding)
    record_user_question(app, original)
    app.state.sessions.task_store.put(
        TaskRecord(other, tool="guarded", invocation_id="inv-original", status="input_required")
    )
    result = await call_input(app, replace(invocation_for(key), task_key=key))
    assert result.action == "accept"
    assert result.content == {"value": "chosen-live-value"}
    assert len(app.state.user_questions) == 2


async def test_ambiguous_legacy_input_declines_without_creating_question(owner: Any) -> None:
    """Do not silently bless a legacy answer under a newly supplied full key."""
    app, key = owner
    question = question_for(key)
    del question.metadata["elicitation"]["task_key"]
    record_user_question(app, question)
    other = replace(key, backend_session_id="other-backend-session")
    app.state.sessions.task_store.put(
        TaskRecord(other, tool="guarded", invocation_id="inv-original", status="input_required")
    )
    result = await call_input(app, replace(invocation_for(key), task_key=key))
    assert result.action == "decline"
    assert list(app.state.user_questions) == [question.id]


async def test_pending_input_rebinds_without_another_created_event(owner: Any) -> None:
    """Recovery parks on the original question and the real answer transition wakes it."""
    app, key = owner
    question = question_for(key, status="pending")
    record_user_question(app, question)
    task = asyncio.create_task(call_input(app, invocation_for(key), timeout=10))
    await asyncio.sleep(0)
    assert not task.done()
    assert list(app.state.user_questions) == [question.id]
    assert not app.state.bus.session_events_since(key.session_id)
    answered = bridge.claim_question_transition(
        app, question.id, "answered", answer_metadata={"value": "after-reconnect"}
    )
    assert answered is not None
    assert bridge.resolve_elicitation(app, answered)
    result = await task
    assert result.content == {"value": "after-reconnect"}


async def test_answer_before_waiter_registration_is_not_lost(
    owner: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An answer with no driver waiter remains durable and visible after attachment."""
    app, key = owner
    question = question_for(key, status="pending")
    record_user_question(app, question)
    original = bridge._register_waiter

    def answer_then_register(app: Any, question_id: str, entry: Any) -> None:
        answered = bridge.claim_question_transition(
            app, question_id, "answered", answer_metadata={"value": "racing-answer"}
        )
        assert answered is not None
        assert not bridge.resolve_elicitation(app, answered)
        original(app, question_id, entry)

    monkeypatch.setattr(bridge, "_register_waiter", answer_then_register)
    result = await call_input(app, invocation_for(key))
    assert result.action == "accept"
    assert result.content == {"value": "racing-answer"}
    assert not app.state.elicitation_waiters


async def test_recoverable_driver_shutdown_preserves_pending_question(owner: Any) -> None:
    """Losing an HTTP observer neither cancels the input nor resets its deadline."""
    app, key = owner
    row = app.state.sessions.task_store.get(key)
    app.state.sessions.task_store.put(replace(row, backend={"transport": "http"}))
    task = asyncio.create_task(call_input(app, replace(invocation_for(key), task_key=key), 10))
    await asyncio.sleep(0)
    question = next(iter(app.state.user_questions.values()))
    assert question.metadata["elicitation"]["task_key"] == key.to_wire()
    assert question.expires_at
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert app.state.user_questions[question.id].status == "pending"
    assert not app.state.elicitation_waiters
    restarted = asyncio.create_task(
        call_input(app, replace(invocation_for(key), task_key=key), 100)
    )
    await asyncio.sleep(0)
    assert app.state.user_questions[question.id].expires_at == question.expires_at
    answered = bridge.claim_question_transition(
        app, question.id, "answered", answer_metadata={"value": "same-question"}
    )
    assert answered is not None and bridge.resolve_elicitation(app, answered)
    assert (await restarted).content == {"value": "same-question"}
    assert (
        sum(
            event.type == "user_question.created"
            for event in app.state.bus.session_events_since(key.session_id)
        )
        == 1
    )


@pytest.mark.parametrize("transport,cancel_requested", [("stdio", False), ("http", True)])
async def test_explicit_or_nonrecoverable_cancellation_settles_input(
    owner: Any,
    transport: str,
    cancel_requested: bool,
) -> None:
    """Recoverability does not turn explicit cancellation into a pending human ask."""
    app, key = owner
    row = app.state.sessions.task_store.get(key)
    app.state.sessions.task_store.put(
        replace(row, backend={"transport": transport}, cancel_requested=cancel_requested)
    )
    task = asyncio.create_task(call_input(app, replace(invocation_for(key), task_key=key), 10))
    await asyncio.sleep(0)
    question = next(iter(app.state.user_questions.values()))
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert app.state.user_questions[question.id].status == "cancelled"
    assert not app.state.elicitation_waiters


async def test_expired_input_returns_cancel_without_accepting_empty_content(owner: Any) -> None:
    """A durable expired question cannot become an accepted form after recovery."""
    app, key = owner
    record_user_question(app, question_for(key, status="expired"))
    result = await call_input(app, invocation_for(key))
    assert result.action == "cancel"
    assert list(app.state.user_questions) == ["original-question"]


async def test_pending_input_keeps_original_elapsed_deadline(owner: Any) -> None:
    """A new observer's larger timeout cannot extend a previously expired deadline."""
    app, key = owner
    question = question_for(key, status="pending").model_copy(
        update={"expires_at": (datetime.now(timezone.utc) - timedelta(seconds=1)).isoformat()}
    )
    record_user_question(app, question)
    result = await call_input(app, invocation_for(key), 100)
    assert result.action == "cancel"
    assert app.state.user_questions[question.id].status == "expired"
    assert len(app.state.user_questions) == 1


def test_input_correlation_uses_bound_full_key_despite_raw_id_collision(owner: Any) -> None:
    """The callback's bound task owner wins over a global raw-id ambiguity."""
    _, key = owner
    invocation = replace(invocation_for(key), input_key=None, task_key=key)
    result = invocation_with_request_correlation(
        invocation, SimpleNamespace(request_id=f"task-{key.task_id}-the-input-key")
    )
    assert result.task_key == key
    assert result.input_key == "the-input-key"
    assert (
        invocation_with_request_correlation(
            invocation, SimpleNamespace(request_id="task-unrelated-backend-input")
        )
        == invocation
    )


async def test_answer_retained_until_backend_receipt_despite_resolved_history_bound(
    owner: Any,
) -> None:
    """An offline driver's answer outlives unrelated resolved history and hot eviction."""
    app, key = owner
    question = question_for(key)
    record_user_question(app, question)
    for index in range(105):
        unrelated = question.model_copy(
            update={
                "id": f"unrelated-{index}",
                "source": "orchestrator",
                "metadata": {},
                "updated_at": (datetime.now(timezone.utc) + timedelta(seconds=index)).isoformat(),
            }
        )
        record_user_question(app, unrelated)
    durable = app.state.sessions.get(key.session_id).metadata["user_questions"]
    assert question.id in durable
    assert len(durable) == 101
    app.state.user_questions.clear()
    assert (await call_input(app, invocation_for(key))).content == {"value": "chosen-live-value"}
    row = app.state.sessions.task_store.get(key)
    app.state.sessions.task_store.put(
        replace(
            row,
            input_answers=(
                TaskInputAnswer(
                    "input-1",
                    {"action": "accept", "content": {"value": "chosen-live-value"}},
                    delivered=True,
                ),
            ),
        )
    )
    record_user_question(
        app,
        question.model_copy(
            update={
                "id": "one-more",
                "source": "orchestrator",
                "metadata": {},
                "updated_at": (datetime.now(timezone.utc) + timedelta(seconds=106)).isoformat(),
            }
        ),
    )
    durable = app.state.sessions.get(key.session_id).metadata["user_questions"]
    assert question.id not in durable
    assert len(durable) == 100


async def test_owner_session_cannot_reuse_another_conversations_answer(owner: Any) -> None:
    """Matching backend ids never grant access to a different conversation's input."""
    app, key = owner
    record_user_question(app, question_for(key))
    other_session = app.state.sessions.create(workspace_id="ws", title="other")
    other = replace(key, session_id=other_session.id)
    app.state.sessions.task_store.put(
        TaskRecord(other, tool="guarded", invocation_id="inv-original", status="input_required")
    )
    result = await call_input(app, replace(invocation_for(other), task_key=other), timeout=0)
    assert result.action == "cancel"
    assert app.state.user_questions["original-question"].status == "answered"
    assert len(app.state.user_questions) == 2


async def test_duplicate_durable_input_records_decline_without_guessing(owner: Any) -> None:
    """Already duplicated legacy history must produce an explicit identity refusal."""
    app, key = owner
    question = question_for(key)
    record_user_question(app, question)
    record_user_question(app, question.model_copy(update={"id": "duplicate-question"}))
    result = await call_input(app, replace(invocation_for(key), task_key=key))
    assert result.action == "decline"
    assert len(app.state.user_questions) == 2


def test_legacy_correlation_uses_app_store_after_another_app_replaces_global(
    owner: Any,
    tmp_path: Path,
) -> None:
    """Constructing another app cannot replace the original task's input identity."""
    app, key = owner
    other = SessionStore(path=tmp_path / "other-app.json")
    assert other.task_store is not None
    result = invocation_with_request_correlation(
        replace(invocation_for(key), task_key=None, input_key=None),
        SimpleNamespace(request_id=f"task-{key.task_id}-input-1"),
        app=app,
    )
    assert result.task_key == key
    assert result.input_key == "input-1"
