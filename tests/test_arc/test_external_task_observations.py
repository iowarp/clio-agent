"""External SDK observations use the one Core task/history and publication path."""

from __future__ import annotations

import asyncio
import hashlib
from types import SimpleNamespace
from typing import Any

import pytest
from fastmcp import Client, FastMCP
from fastmcp_tasks.client_models import ClientGetTaskResult

from clio_agent.arc.memory import ARCMemory
from clio_agent.arc.task_types import (
    BackendReference,
    CoordinationGrant,
    QuestionDefinition,
    TaskOwner,
    TaskSnapshot,
    TaskSubscription,
    argument_digest,
)
from clio_agent.gact.core_recovery import CoreRecovery
from clio_agent.gact.external_tasks import ExternalConnection, ExternalTasks
from clio_agent.gact.questions import QuestionCommands
from clio_agent.gact.task_history import TaskHistory
from clio_agent.tools.task_client import TaskSubmission, restore_native_call

STAMP = "2026-10-14T00:00:00Z"


@pytest.mark.asyncio
@pytest.mark.parametrize("status", ["completed", "failed", "cancelled"])
@pytest.mark.parametrize("failure", ["outcome_mail", "subscriber_mail", "question_mail"])
@pytest.mark.parametrize("restart", [False, True])
async def test_terminal_publication_recovers_after_persistence_without_replaying_execution(
    arc: ARCMemory,
    monkeypatch: pytest.MonkeyPatch,
    status: str,
    failure: str,
    restart: bool,
) -> None:
    """Saved outcomes finish recipient notices and Question closure after each write boundary."""
    history = TaskHistory(arc)
    recovery = CoreRecovery(history)
    app = SimpleNamespace(
        state=SimpleNamespace(
            task_history=history,
            task_recovery=recovery,
            questions=QuestionCommands(history),
            agent=None,
        )
    )
    external = ExternalTasks(app)

    def setup() -> tuple[ExternalConnection, Any, ClientGetTaskResult, str]:
        backend = BackendReference(
            owner="external_mcp",
            server="remote",
            session="original",
            task_id="native-task",
        )
        submission = TaskSubmission(
            "task_result_recovery",
            "work",
            history.write_content("owner", {"arguments": {}}),
            None,
            None,
            argument_digest({}),
        )
        task = TaskSnapshot(
            handle=submission.handle,
            kind="MCP",
            tool="work",
            label="External work",
            owner=TaskOwner(human_id="human", session_id="owner", assignment_id="assignment"),
            invocation_id="original-call",
            backend=backend,
            submission=submission.context,
            status="running",
            created_at=STAMP,
            updated_at=STAMP,
            details=history.write_content("owner", {"requests": {"answer-key": {"params": {}}}}),
        )
        history.record_snapshot(task, observation_id="accepted")
        history.record_grant(
            CoordinationGrant(
                id="human-grant",
                granting_human="human",
                recipient_session="peer",
                target_session="owner",
                target_handle=task.handle,
                rights=("read", "observe"),
                created_at=STAMP,
            ),
            observation_id="human-grant",
        )
        history.record_subscription(
            TaskSubscription(
                handle=task.handle,
                recipient_session="peer",
                recipient_human="human",
                enabled=True,
                updated_at=STAMP,
            )
        )
        question = "task_" + hashlib.sha256((task.handle + "\0answer-key").encode()).hexdigest()
        app.state.questions.create(
            task.owner.model_copy(update={"parent_handle": task.handle}),
            QuestionDefinition(
                prompt="Input for the original external call", source="mcp_elicitation"
            ),
            invocation_id=task.invocation_id,
            handle=question,
        )
        accepted = restore_native_call(
            Client(FastMCP("unused-recovery-transport")),
            submission,
            task_id=backend.task_id,
            status="working",
            created_at=STAMP,
            updated_at=STAMP,
        )
        reply = ClientGetTaskResult.model_validate(
            {
                "resultType": "complete",
                "taskId": backend.task_id,
                "status": status,
                "createdAt": STAMP,
                "lastUpdatedAt": STAMP,
                "result": None
                if status == "cancelled"
                else {
                    "content": [],
                    "structuredContent": {"value": "original result"},
                    "isError": status == "failed",
                },
            }
        )
        return (
            ExternalConnection(backend.model_copy(update={"task_id": ""})),
            accepted,
            reply,
            question,
        )

    connection, accepted, reply, question = await history.io.run(setup)
    fail_notice = (
        "outcome:" + accepted.submission.handle + ":subscriber:peer:1"
        if failure == "subscriber_mail"
        else "outcome:"
        + (question if failure == "question_mail" else accepted.submission.handle)
        + ":owner"
    )
    original = history._record

    def fail_after_terminal(
        event: str,
        sid: str,
        identity: str,
        payload: dict[str, Any],
        *,
        lane: dict[str, Any] | None = None,
    ) -> None:
        if event == "session.mail.accepted" and identity == fail_notice:
            raise OSError("Core unavailable after terminal persistence")
        original(event, sid, identity, payload, lane=lane)

    monkeypatch.setattr(history, "_record", fail_after_terminal)
    try:
        if restart:
            with pytest.raises(OSError, match="after terminal persistence"):
                await history.io.run(external._record_reply, connection, accepted, reply)
        else:
            connection.calls[accepted.task.task_id] = accepted
            await external._observe(connection, accepted.task.task_id, reply)
            assert "external-reply:" + accepted.submission.handle in recovery.pending
        saved = history.task_snapshot(accepted.submission.handle)
        assert saved is not None and saved.status == status and saved.result is not None
        assert saved.terminal_at is not None and history.requires_recovery
        raw = await history.io.run(history.read_content, saved.result)
        assert raw == reply.model_dump(mode="json")
        monkeypatch.setattr(history, "_record", original)
        if restart:
            recovered = TaskHistory(arc)
            await history.io.run(recovered.restore)
            recovered_recovery = CoreRecovery(recovered)
            app.state.task_history, app.state.task_recovery = recovered, recovered_recovery
            app.state.questions = QuestionCommands(recovered)
            replacement = ExternalTasks(app)
            try:
                await replacement.restore()
                await recovered_recovery.drain()
                await replacement.restore()
                assert replacement.connections == {}  # no reconnect or tools/call
            finally:
                await replacement.close()
                await recovered.io.close()
        else:
            recovered = history
            await recovery.drain()
            await history.io.run(external._record_reply, connection, accepted, reply)
        assert recovered.task_snapshot(accepted.submission.handle) == saved
        closed = recovered.task_snapshot(question)
        assert closed is not None and closed.status == "cancelled" and closed.terminal_at
        notices = await history.io.run(recovered.pending_mail, "owner")
        assert {item.id for item in notices} == {
            "outcome:" + accepted.submission.handle + ":owner",
            "outcome:" + question + ":owner",
        }
        assert len(notices) == 2 and not recovered.acknowledged_notifications("owner")
        peer = await history.io.run(recovered.pending_mail, "peer")
        assert len(peer) == 1 and peer[0].id == "outcome:" + saved.handle + ":subscriber:peer:1"
        assert not recovered.acknowledged_notifications("peer")
    finally:
        monkeypatch.setattr(history, "_record", original)
        await recovery.drain()
        await external.close()
        await history.io.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("lost_visibility", [False, True])
@pytest.mark.parametrize("legacy_cancelled", [False, True])
async def test_external_cancelled_reply_records_terminal_without_claiming_cleanup(
    arc: ARCMemory,
    lost_visibility: bool,
    legacy_cancelled: bool,
) -> None:
    """A server's terminal outcome settles its task without inventing remote cleanup proof."""
    history = TaskHistory(arc)
    recovery = CoreRecovery(history)
    owner = ExternalTasks(
        SimpleNamespace(state=SimpleNamespace(task_history=history, task_recovery=recovery))
    )

    def exercise() -> None:
        backend = BackendReference(
            owner="external_mcp", server="remote", session="original", task_id="original-task"
        )
        submission = TaskSubmission(
            "task_cancelled",
            "work",
            history.write_content("owner", {"arguments": {}}),
            None,
            None,
            argument_digest({}),
        )
        current = TaskSnapshot(
            handle=submission.handle,
            kind="MCP",
            owner=TaskOwner(human_id="human", session_id="owner", assignment_id="assignment"),
            backend=backend,
            submission=submission.context,
            tool="work",
            label="External work",
            invocation_id="external-cancel-race",
            created_at=STAMP,
            updated_at=STAMP,
            status="running",
        )
        history.record_snapshot(current, observation_id="accepted")
        accepted = restore_native_call(
            Client(FastMCP("unused-cancel-transport")),
            submission,
            task_id=backend.task_id,
            status="working",
            created_at=STAMP,
            updated_at=STAMP,
        )
        reply = ClientGetTaskResult.model_validate(
            {
                "resultType": "complete",
                "taskId": backend.task_id,
                "status": "cancelled",
                "createdAt": STAMP,
                "lastUpdatedAt": STAMP,
            }
        )
        connection = ExternalConnection(backend.model_copy(update={"task_id": ""}))
        assert current.cancellation_requested_at is None
        if lost_visibility:
            owner._attention(current.handle, "Original server temporarily unavailable")
        if legacy_cancelled:
            prior = history.task_snapshot(current.handle)
            history.record_snapshot(
                prior.model_copy(
                    update={
                        "raw_status": "cancelled",
                        "status": "task_state_uncertain",
                        "settlement": "unknown",
                        "backend_observation": history.write_content(
                            "owner",
                            reply.model_dump(mode="json"),
                        ),
                    }
                ),
                observation_id="legacy-nonterminal-cancellation",
            )
        owner._record_reply(connection, accepted, reply)
        observed = history.task_snapshot(current.handle)
        assert observed is not None and observed.status == "cancelled"
        assert observed.raw_status == "cancelled" and observed.settlement == "external_acknowledged"
        assert observed.terminal_at is not None and observed.result is not None
        assert "cleanup" in observed.progress.message and "unverified" in observed.progress.message
        assert observed.attention_episode is None and observed.connection_freshness == "fresh"
        assert observed.backend_observation is not None
        assert history.read_content(observed.backend_observation)["status"] == "cancelled"
        assert history.read_content(observed.result) == reply.model_dump(mode="json")
        notices = history.pending_mail("owner")
        assert [item.kind for item in notices] == (["attention"] if lost_visibility else []) + [
            "task_result"
        ]
        owner._record_reply(connection, accepted, reply)
        assert history.pending_mail("owner") == notices
        # Protocol terminal results never reopen, even if a server later sends conflicting data.
        owner._record_reply(connection, accepted, reply.model_copy(update={"status": "completed"}))
        assert history.task_snapshot(current.handle) == observed
        assert not history.acknowledged_notifications("owner")

    try:
        await history.io.run(exercise)
    finally:
        await recovery.drain()
        await history.io.close()


@pytest.mark.asyncio
async def test_invalid_external_terminal_is_failed_with_raw_evidence_and_one_owner_notice(
    arc: ARCMemory,
) -> None:
    """Malformed backend output survives recovery; it cannot publish success or duplicate mail."""
    history = TaskHistory(arc)
    recovery = CoreRecovery(history)
    owner = ExternalTasks(
        SimpleNamespace(state=SimpleNamespace(task_history=history, task_recovery=recovery))
    )

    def exercise() -> None:
        backend = BackendReference(
            owner="external_mcp",
            server="contract-server",
            session="native-session",
            auth_scope="verified-human-scope",
            task_id="native-task",
            transport="FastMCPTransport",
        )
        current = TaskSnapshot(
            handle="task_contract",
            kind="MCP",
            tool="work",
            label="Backend work",
            description="Produce an integer",
            description_missing=False,
            owner=TaskOwner(human_id="human", session_id="owner", assignment_id="assignment"),
            invocation_id="invocation",
            backend=backend,
            status="running",
            created_at=STAMP,
            updated_at=STAMP,
        )
        submission = TaskSubmission(
            handle=current.handle,
            tool_name=current.tool,
            tool_version="captured-v1",
            context=history.write_content("owner", {"arguments": {}}),
            argument_digest=argument_digest({}),
            output_schema={
                "type": "object",
                "required": ["value"],
                "properties": {"value": {"type": "integer"}},
            },
        )
        current = current.model_copy(update={"submission": submission.context})
        history.record_snapshot(current, observation_id="accepted")
        client = Client(FastMCP("unused-contract-transport"))
        accepted = restore_native_call(
            client,
            submission,
            task_id="native-task",
            status="working",
            created_at=STAMP,
            updated_at=STAMP,
        )
        raw = {"content": [], "structuredContent": {"value": "not an integer"}, "isError": False}
        reply = ClientGetTaskResult.model_validate(
            {
                "resultType": "complete",
                "taskId": "native-task",
                "status": "completed",
                "createdAt": STAMP,
                "lastUpdatedAt": STAMP,
                "result": raw,
            }
        )
        connection = ExternalConnection(backend.model_copy(update={"task_id": ""}))
        unchanged = history.task_snapshot(current.handle)
        with pytest.raises(
            ValueError, match="differs from its captured submission/backend identity"
        ):
            owner._record_reply(
                connection, accepted, reply.model_copy(update={"task_id": "foreign-task"})
            )
        assert history.task_snapshot(current.handle) == unchanged
        assert not history.pending_mail("owner")
        owner._record_reply(connection, accepted, reply)
        settled = history.task_snapshot(current.handle)
        assert settled is not None
        assert settled.status == "failed" and settled.raw_status == "completed"
        assert settled.terminal_at is not None and settled.settlement == "external_acknowledged"
        assert "declared schema" in settled.progress.message
        assert settled.backend == backend and settled.invocation_id == "invocation"
        assert settled.result is not None
        retained = history.read_content(settled.result)
        assert isinstance(retained, dict) and retained["result"] == raw
        notices = history.pending_mail("owner")
        assert len(notices) == 1 and notices[0].handle == current.handle
        owner._record_reply(connection, accepted, reply)
        assert history.pending_mail("owner") == notices
        restored = TaskHistory(arc)
        restored.restore()
        assert restored.task_snapshot(current.handle) == settled
        assert restored.pending_mail("owner") == notices
        assert history.acknowledged_notifications("owner") == set()

    try:
        await asyncio.to_thread(exercise)
    finally:
        await owner.close()
        await recovery.drain()
