"""A recovered native observation distinguishes known cleanup from missing proof."""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

import pytest
from fastmcp.utilities.tasks import TaskConfig

from clio_agent.arc.memory import ARCMemory
from clio_agent.arc.task_types import (
    BackendReference,
    NativeAttempt,
    SubmissionContext,
    TaskSnapshot,
    argument_digest,
)
from clio_agent.gact.core_recovery import CoreRecovery
from clio_agent.gact.native_identity import local_runtime_identity
from clio_agent.gact.native_tools import NativeTools
from clio_agent.gact.native_work import WorkEvidence
from clio_agent.gact.task_access import TaskCaller
from clio_agent.gact.task_history import TaskHistory
from clio_agent.gact.task_operations import TaskOperations
from tests.test_arc.test_native_owner_contracts import STAMP, configure_roots, owning_assignment


@pytest.mark.asyncio
@pytest.mark.timeout(120)  # Real native SDK/worker cleanup must settle before lifespan exit.
@pytest.mark.parametrize("unknown_cleanup", [False, True])
async def test_recovered_cancelled_reply_keeps_known_cleanup_waiting(
    arc: ARCMemory, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, unknown_cleanup: bool
) -> None:
    """Use the actual native queue, SDK status RPC, worker wrapper and confirmed Core."""
    configure_roots(monkeypatch)
    history = TaskHistory(arc)
    owner = await asyncio.to_thread(owning_assignment, history)
    recovery = CoreRecovery(history)
    entered, cleaning, release, exited = (asyncio.Event() for _ in range(4))
    native = NativeTools(history, lambda _context: None, recovery, admission=lambda _task: None)

    @native.roots["shell"].server.tool(name="held_cleanup", task=TaskConfig(mode="required"))
    async def held_cleanup(_clio_submission: str) -> dict[str, str]:
        async def body(_context: SubmissionContext, evidence: WorkEvidence) -> dict[str, str]:
            evidence.acquiring_effects()
            entered.set()
            try:
                await asyncio.Event().wait()
                return {"unexpected": "the test must cancel this owned operation"}
            finally:
                cleaning.set()
                await release.wait()
                evidence.confirm_cleanup()
                exited.set()

        return await native.work["shell"].run(_clio_submission, "held_cleanup", {}, body)

    async def cancel(task: TaskSnapshot, _caller: TaskCaller) -> dict[str, Any]:
        return await native.cancel(task.handle)

    operations = TaskOperations(history, cancel)
    caller = TaskCaller("owner", "human")
    waiter: asyncio.Task[dict[str, Any]] | None = None
    receipt: dict[str, Any] | None = None
    async with native:
        try:
            receipt = await native.submit(
                "shell",
                "held_cleanup",
                {},
                kind="Shell",
                owner=owner,
                invocation_id="cleanup-visibility",
                workspace=str(tmp_path),
                permissions={},
                confinement={},
                attribution={},
                execution_limits={},
                description="Keep physical cleanup open across observation recovery",
            )
            await asyncio.wait_for(entered.wait(), 5)
            accepted = native._calls[receipt["handle"]]
            await native.cancel(receipt["handle"])
            await asyncio.wait_for(cleaning.wait(), 5)
            assert not exited.is_set()
            await asyncio.to_thread(
                native._uncertain, receipt["handle"], "Observation lost during cleanup", lost=True
            )
            if unknown_cleanup:
                attempts = await asyncio.to_thread(history.native_attempts, receipt["handle"])
                assert len(attempts) == 1
                await asyncio.to_thread(
                    history.record_attempt,
                    attempts[0].model_copy(update={"cleanup_state": "unknown"}),
                )
            # The original SDK handle remains registered while its physical body
            # still needs cleanup. This is a real status RPC, not replayed JSON.
            fresh = await accepted.task.status()
            assert fresh.status == "cancelled"
            snapshot = await asyncio.to_thread(history.task_snapshot, receipt["handle"])
            assert snapshot is not None and snapshot.terminal_at is None
            assert snapshot.connection_freshness == "fresh"
            polled = await operations.wait(caller, receipt["handle"], timeout_s=0)
            assert [row["handle"] for row in polled["unfinished"]] == [receipt["handle"]]
            if unknown_cleanup:
                assert snapshot.status == "task_state_uncertain"
                assert snapshot.attention_episode is not None
                assert polled["outcomes"] == [
                    {
                        "handle": receipt["handle"],
                        "status": "task_state_uncertain",
                    }
                ]
            else:
                assert snapshot.status == "running" and snapshot.settlement == "cleanup_pending"
                assert snapshot.attention_episode is None and polled["outcomes"] == []
                waiter = asyncio.create_task(operations.wait(caller, receipt["handle"]))
                with pytest.raises(TimeoutError):
                    # Diagnostic only: this does not set a product wait timeout.
                    await asyncio.wait_for(asyncio.shield(waiter), 0.2)
                assert not waiter.done() and not exited.is_set()
            assert not history.acknowledged_notifications(owner.session_id)
            release.set()
            settled = await native.await_settlement(receipt["handle"], allow_uncertainty=False)
            assert exited.is_set() and settled.status == "cancelled"
            assert settled.settlement == "confirmed"
            if waiter is not None:
                collected = await asyncio.wait_for(waiter, 5)
                assert collected["unfinished"] == []
                assert collected["outcomes"][0]["status"] == "cancelled"
            assert not history.acknowledged_notifications(owner.session_id)
        finally:
            release.set()
            if waiter is not None and not waiter.done():
                waiter.cancel()
                await asyncio.gather(waiter, return_exceptions=True)
            if receipt is not None:
                await native.cancel(receipt["handle"])
                await native.await_settlement(receipt["handle"], allow_uncertainty=False)
    await recovery.drain()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "evidence",
    ["invalid_result", "foreign_backend", "foreign_runtime", "older_unknown", "confirmed"],
)
async def test_fresh_reply_requires_matching_custody_and_all_cleanup_receipts(
    arc: ARCMemory, monkeypatch: pytest.MonkeyPatch, evidence: str
) -> None:
    """Fresh transport cannot erase missing custody or a captured-contract failure."""
    configure_roots(monkeypatch)
    history = TaskHistory(arc)
    owner = await asyncio.to_thread(owning_assignment, history)
    recovery = CoreRecovery(history)
    native = NativeTools(history, lambda _context: None, recovery, admission=lambda _task: None)

    def exercise() -> None:
        backend = BackendReference(
            owner="native",
            server="shell",
            session="session",
            auth_scope="scope",
            transport="FastMCPTransport",
        )
        context = SubmissionContext(
            reference="internal-reference",
            handle="task_cleanup_evidence",
            owner=owner,
            invocation_id="evidence",
            tool="work",
            argument_digest=argument_digest({}),
            arguments={},
            workspace="workspace",
            permissions={},
            confinement={},
            attribution={},
            execution_limits={},
            backend=backend,
            local_runtime=local_runtime_identity().model_copy(update={"host": "another-runtime"})
            if evidence == "foreign_runtime"
            else local_runtime_identity(),
            output_schema={
                "type": "object",
                "required": ["value"],
                "properties": {"value": {"type": "integer"}},
            },
        )
        submission = history.record_submission(context)
        adopted = backend.model_copy(update={"task_id": "accepted-task"})
        raw = {
            "resultType": "complete",
            "taskId": "accepted-task",
            "status": "completed",
            "createdAt": STAMP,
            "lastUpdatedAt": STAMP,
            "result": {
                "content": [],
                "structuredContent": {"value": "bad" if evidence == "invalid_result" else 1},
                "isError": False,
            },
        }
        observation = history.write_content(owner.session_id, raw)
        current = TaskSnapshot(
            handle=context.handle,
            kind="Shell",
            owner=owner,
            tool="work",
            label="Cleanup evidence",
            invocation_id=context.invocation_id,
            created_at=STAMP,
            updated_at=STAMP,
            submission=submission,
            backend=adopted,
            backend_observation=observation,
            raw_status="completed",
            status="task_state_uncertain",
            settlement="unknown",
            attention_episode="original-attention",
            connection_freshness="fresh",
        )
        if evidence == "foreign_backend":
            current = current.model_copy(
                update={"backend": adopted.model_copy(update={"session": "foreign"})}
            )
        history.record_snapshot(current, observation_id="fresh")
        assert current.backend is not None
        history.record_attempt(
            NativeAttempt(
                handle=context.handle,
                incarnation="current-incarnation",
                backend=current.backend,
                leg=1,
                execution_key="execution",
                started_at=STAMP,
                cleanup_state="confirmed" if evidence == "confirmed" else "pending",
                cleanup_receipt=history.write_content(
                    owner.session_id,
                    {"incarnation": "current-incarnation", "execution_key": "execution"},
                )
                if evidence == "confirmed"
                else None,
                finished_at=STAMP if evidence == "confirmed" else None,
            )
        )
        if evidence == "older_unknown":
            history.record_attempt(
                NativeAttempt(
                    handle=context.handle,
                    incarnation="previous-incarnation",
                    leg=1,
                    backend=adopted,
                    execution_key="older-execution",
                    started_at=STAMP,
                    cleanup_state="unknown",
                )
            )
        if evidence == "foreign_backend":
            with pytest.raises(ValueError, match="differs from original submission custody"):
                native._settle(current, None)
        else:
            native._settle(current, None)
        updated = history.task_snapshot(context.handle)
        assert updated is not None
        if evidence == "confirmed":
            assert updated.status == "completed" and updated.terminal_at is not None
            assert updated.settlement == "confirmed" and updated.result == observation
        else:
            assert updated.status == "task_state_uncertain" and updated.terminal_at is None
            assert updated.attention_episode == "original-attention" and updated.result is None
            assert not history.acknowledged_notifications(owner.session_id)
            if evidence == "invalid_result":
                assert "contract failure" in updated.progress.message

    await asyncio.to_thread(exercise)
    await recovery.drain()
